"""Grounded hybrid search orchestration with canonical parent evidence only."""

from __future__ import annotations

import math
import re
import sqlite3
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.corpus.models import Case, CaseRelation, Chunk, Document, SourceSpan
from src.corpus.storage import StorageError, connect_canonical_storage
from src.retrieval.dense import (
    DenseError,
    DenseSearchFilters,
    DenseSearchHit,
)
from src.retrieval.fusion import (
    FusedHit,
    FusionError,
    calibrated_no_answer_threshold,
    reciprocal_rank_fusion,
)
from src.retrieval.lexical import LexicalError, LexicalHit
from src.retrieval.query import (
    AccessLevel,
    CaseType,
    QueryError,
    QueryFilters,
    normalize_query,
)

_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_MAX_PARENT_CHUNKS = 50
_MAX_PARENT_RELATIONS = 100
_MAX_MATCHED_SPANS = 256
_MAX_QUALITY_WARNINGS = 256
_MAX_WARNING_CHARACTERS = 120
_MAX_SUMMARY_CHARACTERS = 500
_BACKEND_LIMIT = 25
_APPROVED_QUALITY_WARNINGS = frozenset(("below-target-token-range",))


class SearchError(ValueError):
    """A fixed, value-free grounded search boundary failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise SearchError(code) from None


class SearchModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        frozen=True,
    )


class AppliedSearchFilters(SearchModel):
    years: tuple[int, ...] = ()
    domains: tuple[str, ...] = ()
    case_types: tuple[CaseType, ...] = ()
    access_level: AccessLevel


class SearchLatency(SearchModel):
    lexical_ms: float = Field(ge=0)
    dense_ms: float = Field(ge=0)
    fusion_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class MatchedSpan(SearchModel):
    chunk_id: str = Field(min_length=1)
    source_span_index: int = Field(ge=0)
    pdf_page_index: int = Field(ge=1)
    page_label: str | None = None
    bbox: tuple[float, float, float, float]
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_ordered_bbox(self) -> MatchedSpan:
        x0, y0, x1, y1 = self.bbox
        if x0 >= x1 or y0 >= y1:
            raise ValueError("matched span bbox is unordered")
        return self


class SearchResult(SearchModel):
    case_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    question_summary: str | None = None
    edition_year: int = Field(ge=1900, le=2100)
    domain: str = Field(min_length=1)
    part: str = Field(min_length=1)
    case_type: CaseType
    review_status: Literal["search_approved", "approved"]
    currency_status: Literal[
        "unverified", "current", "historical_reference", "superseded"
    ]
    answer_eligible: bool
    answer_context_eligible: bool
    score: float
    reciprocal_rank_score: float
    exact_boost: float
    matched_terms: tuple[str, ...] = Field(default=(), max_length=64)
    matched_spans: tuple[MatchedSpan, ...] = Field(
        default=(), max_length=_MAX_MATCHED_SPANS
    )
    quality_warnings: tuple[str, ...] = Field(
        default=(), max_length=_MAX_QUALITY_WARNINGS
    )
    related_case_ids: tuple[str, ...] = Field(
        default=(), max_length=_MAX_PARENT_RELATIONS
    )
    supersedes_case_ids: tuple[str, ...] = Field(
        default=(), max_length=_MAX_PARENT_RELATIONS
    )
    superseded_by_case_ids: tuple[str, ...] = Field(
        default=(), max_length=_MAX_PARENT_RELATIONS
    )
    conflicts_case_ids: tuple[str, ...] = Field(
        default=(), max_length=_MAX_PARENT_RELATIONS
    )


NoAnswerReason = Literal[
    "no-results",
    "low-fusion-score",
    "missing-evidence",
    "no-approved-answer",
]


class SearchResponse(SearchModel):
    normalized_query: str = Field(min_length=1)
    filters: AppliedSearchFilters
    corpus_version: str = Field(pattern=r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
    lexical_version: str = Field(min_length=1)
    embedding_version: str = Field(min_length=1)
    latency: SearchLatency
    no_answer_candidate: bool
    no_answer_reason_codes: tuple[NoAnswerReason, ...] = Field(default=(), max_length=4)
    results: tuple[SearchResult, ...] = Field(max_length=8)


@dataclass(frozen=True, slots=True)
class SearchParent:
    document: Document
    case: Case
    chunks: tuple[Chunk, ...]
    relations: tuple[CaseRelation, ...] = ()


class _LexicalBackend(Protocol):
    def search(
        self, query: str, *, filters: QueryFilters, limit: int
    ) -> tuple[LexicalHit, ...]: ...


class _DenseBackend(Protocol):
    def search(
        self,
        vector: tuple[float, ...],
        *,
        filters: DenseSearchFilters,
        limit: int,
    ) -> tuple[DenseSearchHit, ...]: ...


class _QueryEncoder(Protocol):
    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class _ParentRepository(Protocol):
    @property
    def corpus_version(self) -> str: ...

    def load(
        self,
        selection: tuple[tuple[str, tuple[str, ...]], ...],
        *,
        filters: QueryFilters,
    ) -> tuple[SearchParent, ...]: ...


def _model_fields(
    value: object, model_type: type[BaseModel]
) -> dict[str, object] | None:
    if type(value) is not model_type:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict or set(fields) != set(model_type.model_fields):
        return None
    return dict(fields)


def _revalidate_document(value: object) -> Document | None:
    fields = _model_fields(value, Document)
    if fields is None:
        return None
    try:
        return Document.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_case(value: object) -> Case | None:
    fields = _model_fields(value, Case)
    if fields is None or type(fields.get("source_spans")) is not tuple:
        return None
    spans: list[dict[str, object]] = []
    for span in cast(tuple[object, ...], fields["source_spans"]):
        checked = _model_fields(span, SourceSpan)
        if checked is None:
            return None
        spans.append(checked)
    fields["source_spans"] = tuple(spans)
    try:
        return Case.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_chunk(value: object) -> Chunk | None:
    fields = _model_fields(value, Chunk)
    if (
        fields is None
        or type(fields.get("source_span_indexes")) is not tuple
        or len(cast(tuple[object, ...], fields["source_span_indexes"]))
        > _MAX_MATCHED_SPANS
        or type(fields.get("quality_flags")) is not tuple
        or len(cast(tuple[object, ...], fields["quality_flags"]))
        > _MAX_QUALITY_WARNINGS
    ):
        return None
    try:
        return Chunk.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_relation(value: object) -> CaseRelation | None:
    fields = _model_fields(value, CaseRelation)
    if fields is None:
        return None
    try:
        return CaseRelation.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def _revalidate_parent(
    value: object,
    *,
    case_id: str,
    chunk_ids: tuple[str, ...],
    filters: QueryFilters,
) -> SearchParent | None:
    if (
        type(value) is not SearchParent
        or type(value.chunks) is not tuple
        or not value.chunks
        or len(value.chunks) > _MAX_PARENT_CHUNKS
        or type(value.relations) is not tuple
        or len(value.relations) > _MAX_PARENT_RELATIONS
    ):
        return None
    document = _revalidate_document(value.document)
    case = _revalidate_case(value.case)
    chunks = tuple(_revalidate_chunk(chunk) for chunk in value.chunks)
    relations = tuple(_revalidate_relation(relation) for relation in value.relations)
    if (
        document is None
        or case is None
        or any(chunk is None for chunk in chunks)
        or any(relation is None for relation in relations)
    ):
        return None
    approved_chunks = cast(tuple[Chunk, ...], chunks)
    approved_relations = cast(tuple[CaseRelation, ...], relations)
    matched_span_count = sum(
        len(chunk.source_span_indexes) for chunk in approved_chunks
    )
    quality_warning_count = sum(len(chunk.quality_flags) for chunk in approved_chunks)
    if (
        case.case_id != case_id
        or case.doc_id != document.doc_id
        or case.extraction_source != document.extraction_method
        or case.review_status not in {"search_approved", "approved"}
        or not case.search_eligible
        or case.pii_class in {"public_credit", "restricted"}
        or (filters.access_level == "public" and document.access_level != "public")
        or (filters.years and document.edition_year not in filters.years)
        or (filters.domains and case.domain not in filters.domains)
        or (filters.case_types and case.case_type not in filters.case_types)
        or matched_span_count > _MAX_MATCHED_SPANS
        or quality_warning_count > _MAX_QUALITY_WARNINGS
        or any(
            span.pdf_page_index > document.pdf_page_count for span in case.source_spans
        )
        or {chunk.chunk_id for chunk in approved_chunks} != set(chunk_ids)
        or len({chunk.chunk_id for chunk in approved_chunks}) != len(approved_chunks)
        or any(
            chunk.case_id != case.case_id
            or not chunk.search_eligible
            or chunk.pii_class != case.pii_class
            or chunk.answer_eligible != case.answer_eligible
            or any(
                index >= len(case.source_spans) for index in chunk.source_span_indexes
            )
            for chunk in approved_chunks
        )
        or any(
            len(warning) > _MAX_WARNING_CHARACTERS
            or warning not in _APPROVED_QUALITY_WARNINGS
            for chunk in approved_chunks
            for warning in chunk.quality_flags
        )
        or any(
            relation.review_status != "approved"
            or case.case_id not in {relation.source_case_id, relation.target_case_id}
            for relation in approved_relations
        )
    ):
        return None
    return SearchParent(
        document=document,
        case=case,
        chunks=approved_chunks,
        relations=approved_relations,
    )


def _model_from_json(payload: object, model_type: type[BaseModel]) -> BaseModel | None:
    if type(payload) is not str or len(payload) > 8 * 1024 * 1024:
        return None
    try:
        return model_type.model_validate_json(payload)
    except (ValidationError, TypeError, ValueError):
        return None


class CanonicalSearchRepository:
    """Read selected parents and their exact evidence from canonical SQLite."""

    def __init__(self, path: Path, *, corpus_version: str) -> None:
        if (
            not isinstance(path, Path)
            or type(corpus_version) is not str
            or _RELEASE_RE.fullmatch(corpus_version) is None
        ):
            _raise("repository_invalid")
        self._path = path
        self._corpus_version = corpus_version

    @property
    def corpus_version(self) -> str:
        return self._corpus_version

    def load(
        self,
        selection: tuple[tuple[str, tuple[str, ...]], ...],
        *,
        filters: QueryFilters,
    ) -> tuple[SearchParent, ...]:
        checked_filters: QueryFilters | None = None
        if type(filters) is QueryFilters:
            try:
                checked_filters = QueryFilters.create(
                    years=filters.years,
                    domains=filters.domains,
                    case_types=filters.case_types,
                    access_level=filters.access_level,
                )
            except QueryError:
                pass
        if (
            checked_filters is None
            or type(selection) is not tuple
            or len(selection) > 8
            or any(
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or not item[0]
                or type(item[1]) is not tuple
                or not item[1]
                or len(item[1]) > _MAX_PARENT_CHUNKS
                or any(
                    type(chunk_id) is not str or not chunk_id for chunk_id in item[1]
                )
                or len(set(item[1])) != len(item[1])
                for item in selection
            )
        ):
            _raise("repository_invalid")
        if len({item[0] for item in selection}) != len(selection):
            _raise("repository_invalid")
        parents: list[SearchParent] = []
        failed = False
        try:
            with connect_canonical_storage(self._path) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                meta_rows = connection.execute(
                    "SELECT release_id FROM build_meta WHERE singleton=1"
                ).fetchmany(2)
                if (
                    len(meta_rows) != 1
                    or meta_rows[0]["release_id"] != self._corpus_version
                ):
                    failed = True
                for case_id, chunk_ids in selection:
                    if failed:
                        break
                    case_rows = connection.execute(
                        "SELECT case_id,doc_id,payload_json FROM cases WHERE case_id=?",
                        (case_id,),
                    ).fetchall()
                    if len(case_rows) != 1:
                        failed = True
                        break
                    case = _model_from_json(case_rows[0]["payload_json"], Case)
                    if (
                        type(case) is not Case
                        or case_rows[0]["case_id"] != case.case_id
                        or case_rows[0]["doc_id"] != case.doc_id
                    ):
                        failed = True
                        break
                    document_rows = connection.execute(
                        "SELECT doc_id,payload_json FROM documents WHERE doc_id=?",
                        (case.doc_id,),
                    ).fetchall()
                    if len(document_rows) != 1:
                        failed = True
                        break
                    document = _model_from_json(
                        document_rows[0]["payload_json"], Document
                    )
                    if type(document) is not Document or document.doc_id != case.doc_id:
                        failed = True
                        break
                    placeholders = ",".join("?" for _ in chunk_ids)
                    chunk_rows = connection.execute(
                        f"SELECT chunk_id,case_id,payload_json FROM chunks WHERE chunk_id IN ({placeholders}) ORDER BY chunk_id",
                        chunk_ids,
                    ).fetchall()
                    chunks: list[Chunk] = []
                    for row in chunk_rows:
                        chunk = _model_from_json(row["payload_json"], Chunk)
                        if (
                            type(chunk) is not Chunk
                            or row["chunk_id"] != chunk.chunk_id
                            or row["case_id"] != chunk.case_id
                            or chunk.case_id != case.case_id
                        ):
                            failed = True
                            break
                        chunks.append(chunk)
                    if failed or {chunk.chunk_id for chunk in chunks} != set(chunk_ids):
                        failed = True
                        break
                    relation_rows = connection.execute(
                        "SELECT relation_id,source_case_id,target_case_id,payload_json FROM case_relations WHERE source_case_id=? OR target_case_id=? ORDER BY relation_id LIMIT ?",
                        (case.case_id, case.case_id, _MAX_PARENT_RELATIONS + 1),
                    ).fetchall()
                    if len(relation_rows) > _MAX_PARENT_RELATIONS:
                        failed = True
                        break
                    relations: list[CaseRelation] = []
                    for row in relation_rows:
                        relation = _model_from_json(row["payload_json"], CaseRelation)
                        if (
                            type(relation) is not CaseRelation
                            or row["relation_id"] != relation.relation_id
                            or row["source_case_id"] != relation.source_case_id
                            or row["target_case_id"] != relation.target_case_id
                            or relation.review_status != "approved"
                        ):
                            failed = True
                            break
                        other_case_id = (
                            relation.target_case_id
                            if relation.source_case_id == case.case_id
                            else relation.source_case_id
                        )
                        other_rows = connection.execute(
                            "SELECT c.case_id,c.doc_id,c.payload_json AS case_payload_json,d.payload_json AS document_payload_json FROM cases AS c JOIN documents AS d ON d.doc_id=c.doc_id WHERE c.case_id=?",
                            (other_case_id,),
                        ).fetchall()
                        if len(other_rows) != 1:
                            failed = True
                            break
                        other_case = _model_from_json(
                            other_rows[0]["case_payload_json"], Case
                        )
                        other_document = _model_from_json(
                            other_rows[0]["document_payload_json"], Document
                        )
                        if (
                            type(other_case) is not Case
                            or type(other_document) is not Document
                            or other_rows[0]["case_id"] != other_case_id
                            or other_case.case_id != other_case_id
                            or other_case.doc_id != other_document.doc_id
                            or other_rows[0]["doc_id"] != other_document.doc_id
                        ):
                            failed = True
                            break
                        if (
                            not other_case.search_eligible
                            or other_case.review_status
                            not in {"search_approved", "approved"}
                            or other_case.pii_class in {"public_credit", "restricted"}
                            or (
                                checked_filters.access_level == "public"
                                and other_document.access_level != "public"
                            )
                        ):
                            continue
                        relations.append(relation)
                    if failed:
                        break
                    parents.append(
                        SearchParent(
                            document=document,
                            case=case,
                            chunks=tuple(chunks),
                            relations=tuple(relations),
                        )
                    )
                if not failed:
                    connection.execute("COMMIT")
        except (OSError, sqlite3.Error, StorageError, TypeError, ValueError):
            failed = True
        if failed or len(parents) != len(selection):
            _raise("repository_invalid")
        return tuple(parents)


def _milliseconds(start: int, end: int) -> float | None:
    if type(start) is not int or type(end) is not int or end < start:
        return None
    value = (end - start) / 1_000_000.0
    return value if math.isfinite(value) else None


def _question_summary(question: str | None) -> str | None:
    if question is None:
        return None
    normalized = " ".join(question.split())
    if len(normalized) <= _MAX_SUMMARY_CHARACTERS:
        return normalized
    return normalized[: _MAX_SUMMARY_CHARACTERS - 1] + "…"


def _relation_ids(
    case_id: str, relations: tuple[CaseRelation, ...]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    related: set[str] = set()
    supersedes: set[str] = set()
    superseded_by: set[str] = set()
    conflicts: set[str] = set()
    for relation in relations:
        other = (
            relation.target_case_id
            if relation.source_case_id == case_id
            else relation.source_case_id
        )
        if relation.relation_type in {"related", "duplicate"}:
            related.add(other)
        elif relation.relation_type == "conflicts":
            conflicts.add(other)
        elif relation.source_case_id == case_id:
            supersedes.add(other)
        else:
            superseded_by.add(other)
    return (
        tuple(sorted(related)),
        tuple(sorted(supersedes)),
        tuple(sorted(superseded_by)),
        tuple(sorted(conflicts)),
    )


def _result_from_parent(fused: FusedHit, parent: SearchParent) -> SearchResult:
    spans: list[MatchedSpan] = []
    evidence_roles: set[str] = set()
    warnings: set[str] = set()
    for chunk in parent.chunks:
        evidence_roles.add(chunk.role)
        warnings.update(chunk.quality_flags)
        for span_index in chunk.source_span_indexes:
            span = parent.case.source_spans[span_index]
            spans.append(
                MatchedSpan(
                    chunk_id=chunk.chunk_id,
                    source_span_index=span_index,
                    pdf_page_index=span.pdf_page_index,
                    page_label=span.page_label,
                    bbox=span.bbox,
                    text_sha256=span.text_sha256,
                )
            )
    related, supersedes, superseded_by, conflicts = _relation_ids(
        parent.case.case_id, parent.relations
    )
    answer_context_eligible = (
        parent.case.review_status == "approved"
        and parent.case.answer_eligible
        and bool(evidence_roles & {"answer", "basis", "facts", "table"})
        and bool(spans)
    )
    return SearchResult(
        case_id=parent.case.case_id,
        doc_id=parent.document.doc_id,
        title=parent.case.title_normalized,
        question_summary=_question_summary(parent.case.question),
        edition_year=parent.document.edition_year,
        domain=parent.case.domain,
        part=parent.case.part,
        case_type=parent.case.case_type,
        review_status=cast(
            Literal["search_approved", "approved"], parent.case.review_status
        ),
        currency_status=parent.case.currency_status,
        answer_eligible=parent.case.answer_eligible,
        answer_context_eligible=answer_context_eligible,
        score=fused.score,
        reciprocal_rank_score=fused.reciprocal_rank_score,
        exact_boost=fused.exact_boost,
        matched_terms=fused.matched_terms,
        matched_spans=tuple(spans),
        quality_warnings=tuple(sorted(warnings)),
        related_case_ids=related,
        supersedes_case_ids=supersedes,
        superseded_by_case_ids=superseded_by,
        conflicts_case_ids=conflicts,
    )


def _revalidate_search_response(value: object) -> SearchResponse | None:
    fields = _model_fields(value, SearchResponse)
    if fields is None or type(fields.get("results")) is not tuple:
        return None
    filter_fields = _model_fields(fields.get("filters"), AppliedSearchFilters)
    latency_fields = _model_fields(fields.get("latency"), SearchLatency)
    if filter_fields is None or latency_fields is None:
        return None
    checked_results: list[dict[str, object]] = []
    for result in cast(tuple[object, ...], fields["results"]):
        result_fields = _model_fields(result, SearchResult)
        if (
            result_fields is None
            or type(result_fields.get("matched_spans")) is not tuple
        ):
            return None
        checked_spans: list[dict[str, object]] = []
        for span in cast(tuple[object, ...], result_fields["matched_spans"]):
            span_fields = _model_fields(span, MatchedSpan)
            if span_fields is None:
                return None
            checked_spans.append(span_fields)
        result_fields["matched_spans"] = tuple(checked_spans)
        checked_results.append(result_fields)
    fields["filters"] = filter_fields
    fields["latency"] = latency_fields
    fields["results"] = tuple(checked_results)
    try:
        return SearchResponse.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


class SearchService:
    """Run both retrieval backends and return only canonical evidence metadata."""

    def __init__(
        self,
        *,
        lexical: _LexicalBackend,
        dense: _DenseBackend,
        encoder: _QueryEncoder,
        repository: _ParentRepository,
        corpus_version: str,
        lexical_version: str,
        embedding_version: str,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        repository_version: object = None
        try:
            repository_version = repository.corpus_version
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        if (
            type(corpus_version) is not str
            or _RELEASE_RE.fullmatch(corpus_version) is None
            or type(lexical_version) is not str
            or _VERSION_RE.fullmatch(lexical_version) is None
            or type(embedding_version) is not str
            or _VERSION_RE.fullmatch(embedding_version) is None
            or repository_version != corpus_version
            or not callable(clock_ns)
        ):
            _raise("service_invalid")
        self.lexical = lexical
        self.dense = dense
        self._encoder = encoder
        self._repository = repository
        self._corpus_version = corpus_version
        self._lexical_version = lexical_version
        self._embedding_version = embedding_version
        self._clock_ns = clock_ns

    def _timed_lexical(
        self, query: str, filters: QueryFilters
    ) -> tuple[tuple[LexicalHit, ...], float]:
        start = self._clock_ns()
        hits = self.lexical.search(query, filters=filters, limit=_BACKEND_LIMIT)
        elapsed = _milliseconds(start, self._clock_ns())
        if elapsed is None:
            _raise("search_clock_invalid")
        return hits, elapsed

    def _timed_dense(
        self, vector: tuple[float, ...], filters: DenseSearchFilters
    ) -> tuple[tuple[DenseSearchHit, ...], float]:
        start = self._clock_ns()
        hits = self.dense.search(vector, filters=filters, limit=_BACKEND_LIMIT)
        elapsed = _milliseconds(start, self._clock_ns())
        if elapsed is None:
            _raise("search_clock_invalid")
        return hits, elapsed

    def search(
        self,
        query: str,
        *,
        years: tuple[int, ...] = (),
        domains: tuple[str, ...] = (),
        case_types: tuple[CaseType, ...] = (),
        access_level: AccessLevel = "public",
        limit: int = 8,
    ) -> SearchResponse:
        if type(limit) is not int or limit < 1 or limit > 8:
            _raise("search_invalid")
        total_start = self._clock_ns()
        filters: QueryFilters | None = None
        normalized = None
        try:
            filters = QueryFilters.create(
                years=years,
                domains=domains,
                case_types=case_types,
                access_level=access_level,
            )
            normalized = normalize_query(query, filters=filters)
        except QueryError:
            pass
        if filters is None or normalized is None:
            _raise("search_invalid")
        failed = False
        lexical_result: tuple[tuple[LexicalHit, ...], float] | None = None
        dense_result: tuple[tuple[DenseSearchHit, ...], float] | None = None
        vector: tuple[float, ...] | None = None
        try:
            vectors = self._encoder.encode((normalized.text,))
            if type(vectors) is tuple and len(vectors) == 1:
                vector = vectors[0]
            if vector is None:
                failed = True
            else:
                dense_filters = DenseSearchFilters.create(
                    years=filters.years,
                    domains=filters.domains,
                    case_types=filters.case_types,
                    access_level=filters.access_level,
                )
                with ThreadPoolExecutor(max_workers=2) as executor:
                    lexical_future = executor.submit(
                        self._timed_lexical, normalized.text, filters
                    )
                    dense_future = executor.submit(
                        self._timed_dense, vector, dense_filters
                    )
                    lexical_result = lexical_future.result()
                    dense_result = dense_future.result()
        except (
            DenseError,
            FusionError,
            LexicalError,
            QueryError,
            SearchError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            failed = True
        if failed or lexical_result is None or dense_result is None:
            _raise("search_backend_failed")
        fusion_start = self._clock_ns()
        fused: tuple[FusedHit, ...] | None = None
        parents: tuple[SearchParent, ...] | None = None
        try:
            fused = reciprocal_rank_fusion(
                lexical_result[0],
                dense_result[0],
                exact_tokens=normalized.exact_tokens,
                k=60,
                limit=limit,
            )
            selection = tuple((hit.case_id, hit.chunk_ids) for hit in fused)
            parents = (
                self._repository.load(selection, filters=filters) if selection else ()
            )
        except (FusionError, SearchError, OSError, RuntimeError, TypeError, ValueError):
            failed = True
        fusion_end = self._clock_ns()
        fusion_ms = _milliseconds(fusion_start, fusion_end)
        if (
            failed
            or fused is None
            or parents is None
            or fusion_ms is None
            or len(parents) != len(fused)
        ):
            _raise("search_evidence_failed")
        results: list[SearchResult] = []
        for hit, parent in zip(fused, parents, strict=True):
            checked_parent = _revalidate_parent(
                parent,
                case_id=hit.case_id,
                chunk_ids=hit.chunk_ids,
                filters=filters,
            )
            if checked_parent is None:
                _raise("search_evidence_failed")
            results.append(_result_from_parent(hit, checked_parent))
        reason_codes: list[NoAnswerReason] = []
        if not results:
            reason_codes.append("no-results")
        else:
            if results[0].reciprocal_rank_score < calibrated_no_answer_threshold(
                normalized.exact_tokens
            ):
                reason_codes.append("low-fusion-score")
            if not any(result.matched_spans for result in results):
                reason_codes.append("missing-evidence")
            if not any(result.answer_context_eligible for result in results):
                reason_codes.append("no-approved-answer")
        total_ms = _milliseconds(total_start, self._clock_ns())
        if total_ms is None:
            _raise("search_clock_invalid")
        try:
            return SearchResponse(
                normalized_query=normalized.text,
                filters=AppliedSearchFilters(
                    years=filters.years,
                    domains=filters.domains,
                    case_types=filters.case_types,
                    access_level=filters.access_level,
                ),
                corpus_version=self._corpus_version,
                lexical_version=self._lexical_version,
                embedding_version=self._embedding_version,
                latency=SearchLatency(
                    lexical_ms=lexical_result[1],
                    dense_ms=dense_result[1],
                    fusion_ms=fusion_ms,
                    total_ms=total_ms,
                ),
                no_answer_candidate=bool(reason_codes),
                no_answer_reason_codes=tuple(reason_codes),
                results=tuple(results),
            )
        except (ValidationError, TypeError, ValueError):
            pass
        _raise("search_response_invalid")

    def select_answer_context(
        self, response: object, *, limit: int = 5
    ) -> tuple[SearchResult, ...]:
        checked_response = _revalidate_search_response(response)
        if checked_response is None or type(limit) is not int or limit < 1 or limit > 5:
            _raise("answer_context_invalid")
        return tuple(
            result
            for result in checked_response.results
            if result.answer_context_eligible
            and result.answer_eligible
            and result.review_status == "approved"
            and bool(result.matched_spans)
        )[:limit]
