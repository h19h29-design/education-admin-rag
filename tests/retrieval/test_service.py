from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from typer.testing import CliRunner

from src.cli import app
from src.corpus.models import CaseRelation, SourceSpan
from src.retrieval.dense import DenseSearchFilters, DenseSearchHit
from src.retrieval.lexical import LexicalHit
from src.retrieval.query import AccessLevel, QueryFilters
from src.retrieval.service import (
    CanonicalSearchRepository,
    SearchError,
    SearchParent,
    SearchResponse,
    SearchService,
)
from tests.retrieval.test_lexical import (
    CASE_PUBLIC,
    CASE_STAFF,
    _case,
    _chunk,
    _document,
)

RELEASE_ID = "corpus-20250808123456-deadbeef"


def _write_search_database(path: Path) -> None:
    document = _document(year=2025, access_level="public")
    case = _case(
        CASE_PUBLIC,
        document=document,
        domain="계약",
        title="2단계 입찰",
        question="지방계약법 제12조의 기준은 무엇인가요?",
    )
    chunk = _chunk(case)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(
            """
            CREATE TABLE build_meta(singleton INTEGER PRIMARY KEY,release_id TEXT NOT NULL);
            CREATE TABLE documents(doc_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL);
            CREATE TABLE cases(case_id TEXT PRIMARY KEY,doc_id TEXT NOT NULL,payload_json TEXT NOT NULL);
            CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,payload_json TEXT NOT NULL);
            CREATE TABLE case_relations(relation_id TEXT PRIMARY KEY,source_case_id TEXT NOT NULL,target_case_id TEXT NOT NULL,payload_json TEXT NOT NULL);
            """
        )
        connection.execute("INSERT INTO build_meta VALUES(1,?)", (RELEASE_ID,))
        connection.execute(
            "INSERT INTO documents VALUES(?,?)",
            (document.doc_id, document.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO cases VALUES(?,?,?)",
            (case.case_id, case.doc_id, case.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO chunks VALUES(?,?,?)",
            (chunk.chunk_id, chunk.case_id, chunk.model_dump_json()),
        )


class FakeLexical:
    def __init__(self, hits: tuple[LexicalHit, ...]) -> None:
        self.hits = hits
        self.last_filter: QueryFilters | None = None
        self.fail = False

    def search(
        self, query: str, *, filters: QueryFilters, limit: int
    ) -> tuple[LexicalHit, ...]:
        self.last_filter = filters
        if self.fail:
            raise RuntimeError("PRIVATE_LEXICAL_SENTINEL")
        return self.hits[:limit]


class FakeDense:
    def __init__(self, hits: tuple[DenseSearchHit, ...]) -> None:
        self.hits = hits
        self.last_filter: DenseSearchFilters | None = None

    def search(
        self,
        vector: tuple[float, ...],
        *,
        filters: DenseSearchFilters,
        limit: int,
    ) -> tuple[DenseSearchHit, ...]:
        assert vector == (0.6, 0.8)
        self.last_filter = filters
        return self.hits[:limit]


class FakeEncoder:
    def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        assert len(texts) == 1
        return ((0.6, 0.8),)


class FakeRepository:
    def __init__(self, parents: tuple[SearchParent, ...]) -> None:
        self.parents = {parent.case.case_id: parent for parent in parents}
        self.last_selection: object = None
        self.corpus_version = RELEASE_ID

    def load(
        self,
        selection: tuple[tuple[str, tuple[str, ...]], ...],
        *,
        filters: QueryFilters,
    ) -> tuple[SearchParent, ...]:
        self.last_selection = (selection, filters)
        return tuple(
            self.parents[case_id] for case_id, _ in selection if case_id in self.parents
        )


def _fixture_service(
    *, status: str = "approved", document_access: AccessLevel = "public"
) -> SearchService:
    document = _document(year=2025, access_level=document_access)
    case = _case(
        CASE_PUBLIC,
        document=document,
        domain="계약",
        title="2단계 입찰",
        question="지방계약법 제12조의 기준은 무엇인가요?",
        status=status,
    )
    chunk = _chunk(case)
    lexical_hit = LexicalHit(
        chunk_id=chunk.chunk_id,
        case_id=case.case_id,
        doc_id=document.doc_id,
        score=10.0,
        matched_terms=("제12조",),
        review_status=status,  # type: ignore[arg-type]
        answer_eligible=case.answer_eligible,
    )
    dense_hit = DenseSearchHit(
        point_id="point-public",
        chunk_id=chunk.chunk_id,
        case_id=case.case_id,
        score=0.9,
    )
    relation = CaseRelation(
        relation_id="relation-public-new",
        source_case_id="senqa-2025-contract-general-2",
        target_case_id=case.case_id,
        relation_type="supersedes",
        confidence=1.0,
        review_status="approved",
    )
    parent = SearchParent(
        document=document,
        case=case,
        chunks=(chunk,),
        relations=(relation,),
    )
    return SearchService(
        lexical=FakeLexical((lexical_hit,)),
        dense=FakeDense((dense_hit,)),
        encoder=FakeEncoder(),
        repository=FakeRepository((parent,)),
        corpus_version=RELEASE_ID,
        lexical_version="korean-lexical-v1",
        embedding_version="bge-m3-5617a9f6",
        clock_ns=lambda: 0,
    )


def test_search_returns_evidence_spans_and_never_answer_text() -> None:
    service = _fixture_service()

    response = service.search("2단계 입찰 제12조", access_level="staff")

    assert response.results[0].matched_spans
    assert response.results[0].matched_spans[0].pdf_page_index >= 1
    assert response.results[0].matched_spans[0].source_span_index == 0
    assert response.results[0].doc_id == "doc-2025-public"
    assert not hasattr(response, "generated_answer")
    rendered = response.model_dump(mode="json")
    assert "generated_answer" not in rendered
    assert "answer" not in rendered["results"][0]


def test_policy_filters_are_passed_to_both_backends_before_search() -> None:
    service = _fixture_service()

    service.search(
        "감사 사례",
        years=(2025,),
        domains=("계약",),
        case_types=("qa",),
        access_level="staff",
    )

    assert cast(FakeLexical, service.lexical).last_filter == QueryFilters.create(
        years=(2025,), domains=("계약",), case_types=("qa",), access_level="staff"
    )
    assert cast(FakeDense, service.dense).last_filter == DenseSearchFilters.create(
        years=(2025,), domains=("계약",), case_types=("qa",), access_level="staff"
    )


def test_public_search_rejects_a_staff_parent_even_if_both_backends_return_it() -> None:
    service = _fixture_service(document_access="staff")

    with pytest.raises(SearchError, match="search_evidence_failed") as captured:
        service.search("입찰", access_level="public")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_search_service_rejects_a_repository_from_another_corpus_release() -> None:
    original = _fixture_service()
    repository = cast(FakeRepository, original._repository)
    repository.corpus_version = "corpus-20250808123456-cafebabe"

    with pytest.raises(SearchError, match="service_invalid") as captured:
        SearchService(
            lexical=original.lexical,
            dense=original.dense,
            encoder=FakeEncoder(),
            repository=repository,
            corpus_version=RELEASE_ID,
            lexical_version="korean-lexical-v1",
            embedding_version="bge-m3-5617a9f6",
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_answer_context_selector_requires_answer_eligibility_and_approval() -> None:
    approved = _fixture_service(status="approved")
    search_only = _fixture_service(status="search_approved")

    approved_context = approved.select_answer_context(
        approved.search("감사 사례", access_level="staff"), limit=5
    )
    search_only_context = search_only.select_answer_context(
        search_only.search("감사 사례", access_level="staff"), limit=5
    )

    assert approved_context
    assert all(item.answer_eligible is True for item in approved_context)
    assert all(item.review_status == "approved" for item in approved_context)
    assert search_only_context == ()


def test_approved_supersedes_relation_is_preserved_without_latest_year_boost() -> None:
    response = _fixture_service().search("입찰", access_level="public")

    assert response.results[0].superseded_by_case_ids == (
        "senqa-2025-contract-general-2",
    )
    assert response.results[0].edition_year == 2025


def test_backend_failure_never_returns_partial_results_or_private_values() -> None:
    service = _fixture_service()
    cast(FakeLexical, service.lexical).fail = True

    with pytest.raises(SearchError, match="search_backend_failed") as captured:
        service.search("PRIVATE_QUERY_SENTINEL", access_level="public")

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert "PRIVATE" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_forged_repository_parent_is_a_fixed_evidence_error() -> None:
    service = _fixture_service()
    repository = cast(FakeRepository, service._repository)
    original = repository.parents[CASE_PUBLIC]
    repository.parents[CASE_PUBLIC] = SearchParent(
        document=original.document,
        case=cast(Any, object()),
        chunks=original.chunks,
        relations=original.relations,
    )

    with pytest.raises(SearchError, match="search_evidence_failed") as captured:
        service.search("입찰")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_question_type_calibration_uses_exact_evidence_not_a_single_threshold() -> None:
    general = _fixture_service()
    exact = _fixture_service()
    cast(FakeLexical, general.lexical).hits = ()
    cast(FakeLexical, exact.lexical).hits = ()

    general_response = general.search("입찰 절차")
    exact_response = exact.search(CASE_PUBLIC)

    assert "low-fusion-score" in general_response.no_answer_reason_codes
    assert "low-fusion-score" not in exact_response.no_answer_reason_codes


def test_search_rejects_out_of_document_or_unbounded_matched_spans() -> None:
    service = _fixture_service()
    repository = cast(FakeRepository, service._repository)
    original = repository.parents[CASE_PUBLIC]
    excessive_spans = tuple(
        SourceSpan(
            pdf_page_index=13,
            page_label="13",
            bbox=(0.0, float(index), 1.0, float(index + 1)),
            text_sha256=f"{index:064x}",
        )
        for index in range(257)
    )
    repository.parents[CASE_PUBLIC] = SearchParent(
        document=original.document,
        case=original.case.model_copy(update={"source_spans": excessive_spans}),
        chunks=(
            original.chunks[0].model_copy(
                update={"source_span_indexes": tuple(range(257))}
            ),
        ),
        relations=original.relations,
    )

    with pytest.raises(SearchError, match="search_evidence_failed") as captured:
        service.search("입찰")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None

    out_of_document = SourceSpan(
        pdf_page_index=original.document.pdf_page_count + 1,
        page_label="31",
        bbox=(0.0, 0.0, 1.0, 1.0),
        text_sha256="f" * 64,
    )
    repository.parents[CASE_PUBLIC] = SearchParent(
        document=original.document,
        case=original.case.model_copy(update={"source_spans": (out_of_document,)}),
        chunks=original.chunks,
        relations=original.relations,
    )

    with pytest.raises(SearchError, match="search_evidence_failed") as captured:
        service.search("입찰")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_search_never_reflects_unapproved_quality_warning_values() -> None:
    service = _fixture_service()
    repository = cast(FakeRepository, service._repository)
    original = repository.parents[CASE_PUBLIC]
    repository.parents[CASE_PUBLIC] = SearchParent(
        document=original.document,
        case=original.case,
        chunks=(
            original.chunks[0].model_copy(
                update={"quality_flags": ("PRIVATE_WARNING_SENTINEL",)}
            ),
        ),
        relations=original.relations,
    )

    with pytest.raises(SearchError, match="search_evidence_failed") as captured:
        service.search("입찰")

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert "PRIVATE" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_answer_context_selector_recursively_revalidates_frozen_response() -> None:
    service = _fixture_service()
    response = service.search("입찰")
    result = response.results[0]
    forged_result = type(result).model_construct(
        **{
            **object.__getattribute__(result, "__dict__"),
            "matched_spans": (cast(Any, object()),),
        }
    )
    forged_response = SearchResponse.model_construct(
        **{
            **object.__getattribute__(response, "__dict__"),
            "results": (forged_result,),
        }
    )

    with pytest.raises(SearchError, match="answer_context_invalid") as captured:
        service.select_answer_context(forged_response)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_answer_context_selector_rejects_an_unordered_evidence_bbox() -> None:
    service = _fixture_service()
    response = service.search("입찰")
    result = response.results[0]
    span = result.matched_spans[0]
    forged_span = type(span).model_construct(
        **{
            **object.__getattribute__(span, "__dict__"),
            "bbox": (1.0, 1.0, 0.0, 0.0),
        }
    )
    forged_result = type(result).model_construct(
        **{
            **object.__getattribute__(result, "__dict__"),
            "matched_spans": (forged_span,),
        }
    )
    forged_response = SearchResponse.model_construct(
        **{
            **object.__getattribute__(response, "__dict__"),
            "results": (forged_result,),
        }
    )

    with pytest.raises(SearchError, match="answer_context_invalid") as captured:
        service.select_answer_context(forged_response)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_canonical_repository_rebinds_case_chunk_document_and_span(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_search_database(database)
    repository = CanonicalSearchRepository(database, corpus_version=RELEASE_ID)

    parents = repository.load(
        ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
        filters=QueryFilters.create(),
    )

    assert len(parents) == 1
    assert parents[0].case.case_id == CASE_PUBLIC
    assert parents[0].chunks[0].source_span_indexes == (0,)
    assert parents[0].case.source_spans[0].pdf_page_index == 13


def test_canonical_repository_rejects_cross_case_chunk_payload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_search_database(database)
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM chunks WHERE chunk_id=?",
                (f"chunk-{CASE_PUBLIC}",),
            ).fetchone()[0]
        )
        payload["case_id"] = "PRIVATE_CROSS_CASE_SENTINEL"
        connection.execute(
            "UPDATE chunks SET payload_json=? WHERE chunk_id=?",
            (json.dumps(payload), f"chunk-{CASE_PUBLIC}"),
        )
    repository = CanonicalSearchRepository(database, corpus_version=RELEASE_ID)

    with pytest.raises(SearchError, match="repository_invalid") as captured:
        repository.load(
            ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
            filters=QueryFilters.create(),
        )

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert "PRIVATE" not in rendered


def test_public_repository_hides_relations_to_staff_only_cases(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_search_database(database)
    staff_document = _document(year=2024, access_level="staff")
    staff_case = _case(
        CASE_STAFF,
        document=staff_document,
        domain="재정",
        title="내부 학교회계 사례",
        question="내부 검토 사항인가요?",
    )
    staff_chunk = _chunk(staff_case)
    relation = CaseRelation(
        relation_id="relation-staff-public",
        source_case_id=staff_case.case_id,
        target_case_id=CASE_PUBLIC,
        relation_type="conflicts",
        confidence=1.0,
        review_status="approved",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO documents VALUES(?,?)",
            (staff_document.doc_id, staff_document.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO cases VALUES(?,?,?)",
            (staff_case.case_id, staff_case.doc_id, staff_case.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO chunks VALUES(?,?,?)",
            (staff_chunk.chunk_id, staff_chunk.case_id, staff_chunk.model_dump_json()),
        )
        connection.execute(
            "INSERT INTO case_relations VALUES(?,?,?,?)",
            (
                relation.relation_id,
                relation.source_case_id,
                relation.target_case_id,
                relation.model_dump_json(),
            ),
        )
    repository = CanonicalSearchRepository(database, corpus_version=RELEASE_ID)

    public_parent = repository.load(
        ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
        filters=QueryFilters.create(access_level="public"),
    )[0]
    staff_parent = repository.load(
        ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
        filters=QueryFilters.create(access_level="staff"),
    )[0]

    assert public_parent.relations == ()
    assert [item.relation_id for item in staff_parent.relations] == [
        "relation-staff-public"
    ]


def test_canonical_repository_binds_the_claimed_corpus_release(tmp_path: Path) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_search_database(database)
    repository = CanonicalSearchRepository(
        database, corpus_version="corpus-20250808123456-cafebabe"
    )

    with pytest.raises(SearchError, match="repository_invalid") as captured:
        repository.load(
            ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
            filters=QueryFilters.create(),
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_repository_hides_relations_to_restricted_or_unsearchable_cases(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_search_database(database)
    public_document = _document(year=2025, access_level="public")
    restricted_case = _case(
        "senqa-2025-contract-contract-general-99",
        document=public_document,
        domain="계약",
        title="제한 사례",
        question="제한 사례입니까?",
        pii_class="restricted",
    )
    relation = CaseRelation(
        relation_id="relation-restricted-public",
        source_case_id=restricted_case.case_id,
        target_case_id=CASE_PUBLIC,
        relation_type="related",
        confidence=1.0,
        review_status="approved",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO cases VALUES(?,?,?)",
            (
                restricted_case.case_id,
                restricted_case.doc_id,
                restricted_case.model_dump_json(),
            ),
        )
        connection.execute(
            "INSERT INTO case_relations VALUES(?,?,?,?)",
            (
                relation.relation_id,
                relation.source_case_id,
                relation.target_case_id,
                relation.model_dump_json(),
            ),
        )
    repository = CanonicalSearchRepository(database, corpus_version=RELEASE_ID)

    parent = repository.load(
        ((CASE_PUBLIC, (f"chunk-{CASE_PUBLIC}",)),),
        filters=QueryFilters.create(access_level="staff"),
    )[0]

    assert parent.relations == ()


def test_search_response_schema_is_checked_in_and_cli_reproducible(
    tmp_path: Path,
) -> None:
    checked_in = json.loads(
        Path("data/schemas/search-result.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(checked_in)
    generated = tmp_path / "schemas"

    result = CliRunner().invoke(app, ["export-schemas", "--output", str(generated)])

    assert result.exit_code == 0
    assert (
        json.loads(
            (generated / "search-result.schema.json").read_text(encoding="utf-8")
        )
        == checked_in
    )
    Draft202012Validator(checked_in).validate(
        _fixture_service().search("입찰").model_dump(mode="json")
    )
    assert SearchResponse.model_json_schema()["additionalProperties"] is False
