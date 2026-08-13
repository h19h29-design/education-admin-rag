from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import src.retrieval.lexical as lexical_module
from src.corpus.models import Case, Chunk, Document, LawRef, SourceSpan
from src.retrieval.lexical import (
    LexicalError,
    LexicalIndex,
    build_lexical_index,
    inspect_lexical_index,
)
from src.retrieval.query import QueryFilters

CASE_PUBLIC = "senqa-2025-contract-contract-general-1"
CASE_STAFF = "senqa-2024-finance-finance-general-2"
CASE_PENDING = "senqa-2025-contract-contract-general-3"
CASE_RESTRICTED = "senqa-2025-contract-contract-general-4"


def _span(seed: str, page: int = 13) -> SourceSpan:
    return SourceSpan(
        pdf_page_index=page,
        page_label=str(page),
        bbox=(10.0, 20.0, 100.0, 200.0),
        text_sha256=seed * 64,
    )


def _document(*, year: int, access_level: str) -> Document:
    return Document(
        doc_id=f"doc-{year}-{access_level}",
        edition_year=year,
        title=f"{year} 사례집",
        publisher="서울특별시교육청",
        source_filename=f"{year}-{access_level}.pdf",
        sha256=("a" if access_level == "public" else "b") * 64,
        pdf_page_count=30,
        extraction_method="native",
        redistribution_status="approved",
        access_level=access_level,  # type: ignore[arg-type]
        page_numbering_rule="pdf-index",
        ingestion_version="test-v1",
    )


def _case(
    case_id: str,
    *,
    document: Document,
    domain: str,
    title: str,
    question: str,
    status: str = "approved",
    pii_class: str = "none",
    law_ref_ids: tuple[str, ...] = (),
) -> Case:
    searchable = status in {"approved", "search_approved"} and pii_class != "restricted"
    return Case(
        case_id=case_id,
        doc_id=document.doc_id,
        case_type="qa",
        domain=domain,
        part=domain,
        case_no=case_id.rsplit("-", 1)[-1],
        title_raw=title,
        title_normalized=title,
        question=question,
        answer="예정가격 이하로 입찰한 사례입니다.",
        basis_text="「지방계약법」 제12조제3항에 따릅니다.",
        law_ref_ids=law_ref_ids,
        source_spans=(_span(case_id[-1]),),
        extraction_source="native",
        extraction_confidence=1.0,
        critical_field_review="verified",
        pii_class=pii_class,  # type: ignore[arg-type]
        anonymization_status="not-required",
        currency_status="current",
        search_eligible=searchable,
        answer_eligible=searchable and status == "approved",
        review_status=status,  # type: ignore[arg-type]
    )


def _chunk(case: Case) -> Chunk:
    return Chunk(
        chunk_id=f"chunk-{case.case_id}",
        case_id=case.case_id,
        role="answer",
        sequence=1,
        text=case.answer or "답변 없음",
        embedding_text=case.answer or "답변 없음",
        source_span_indexes=(0,),
        token_count=20,
        pii_class=case.pii_class,
        search_eligible=case.search_eligible,
        answer_eligible=case.answer_eligible,
    )


def _law_ref(case: Case) -> LawRef:
    return LawRef(
        law_ref_id=f"law-{case.case_id}",
        case_id=case.case_id,
        display_name="지방계약법",
        article="제12조",
        paragraph="제3항",
        quote="「지방계약법」 제12조제3항",
        source_span=case.source_spans[0],
        parsing_confidence=1.0,
        currency_status="current",
        review_status="approved",
    )


def _write_canonical_database(path: Path) -> None:
    public_doc = _document(year=2025, access_level="public")
    staff_doc = _document(year=2024, access_level="staff")
    cases = (
        _case(
            CASE_PUBLIC,
            document=public_doc,
            domain="계약",
            title="2단계 입찰 예정가격 1,502,000원",
            question="2단계 입찰에 지방계약법 제12조제3항을 적용하나요?",
            law_ref_ids=(f"law-{CASE_PUBLIC}",),
        ),
        _case(
            CASE_STAFF,
            document=staff_doc,
            domain="재정",
            title="학교회계 예산 편성",
            question="학교회계 이월금을 어떻게 처리하나요?",
            status="search_approved",
        ),
        _case(
            CASE_PENDING,
            document=public_doc,
            domain="계약",
            title="검토 중인 2단계 입찰",
            question="아직 승인되지 않았나요?",
            status="needs_review",
        ),
        _case(
            CASE_RESTRICTED,
            document=public_doc,
            domain="계약",
            title="제한 자료 2단계 입찰",
            question="제한 자료입니다.",
            pii_class="restricted",
        ),
    )
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE build_meta(singleton INTEGER PRIMARY KEY, release_id TEXT NOT NULL);
        CREATE TABLE documents(doc_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE cases(case_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE law_refs(law_ref_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, payload_json TEXT NOT NULL);
        """
    )
    connection.execute("INSERT INTO build_meta VALUES(1,?)", ("corpus-test",))
    for document in (public_doc, staff_doc):
        connection.execute(
            "INSERT INTO documents VALUES(?,?)",
            (document.doc_id, document.model_dump_json()),
        )
    for case in cases:
        connection.execute(
            "INSERT INTO cases VALUES(?,?)", (case.case_id, case.model_dump_json())
        )
        chunk = _chunk(case)
        connection.execute(
            "INSERT INTO chunks VALUES(?,?,?)",
            (chunk.chunk_id, chunk.case_id, chunk.model_dump_json()),
        )
    public_case = cases[0]
    law_ref = _law_ref(public_case)
    connection.execute(
        "INSERT INTO law_refs VALUES(?,?,?)",
        (law_ref.law_ref_id, law_ref.case_id, law_ref.model_dump_json()),
    )
    connection.commit()
    connection.close()


@pytest.fixture
def lexical_index(tmp_path: Path) -> LexicalIndex:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    result = build_lexical_index(canonical, target)
    assert result.indexed_chunks == 2
    assert result.skipped_chunks == 2
    return LexicalIndex(target)


def test_inspect_lexical_index_revalidates_release_and_exact_record_count(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    built = build_lexical_index(canonical, target)

    inspected = inspect_lexical_index(target)

    assert inspected.release_id == built.release_id
    assert inspected.indexed_chunks == built.indexed_chunks
    assert inspected.config_sha256 == built.config_sha256


def test_spacing_variant_and_exact_article_are_retrieved(
    lexical_index: LexicalIndex,
) -> None:
    hits = lexical_index.search("2단계입찰 제12조제3항 1,502,000원", limit=10)

    assert hits[0].case_id == CASE_PUBLIC
    assert "제12조제3항" in hits[0].matched_terms
    assert "1,502,000원" in hits[0].matched_terms


def test_default_public_filter_excludes_staff_and_unapproved_records(
    lexical_index: LexicalIndex,
) -> None:
    public_hits = lexical_index.search("학교회계 입찰", limit=10)

    assert {hit.case_id for hit in public_hits} <= {CASE_PUBLIC}
    assert CASE_STAFF not in {hit.case_id for hit in public_hits}
    assert CASE_PENDING not in {hit.case_id for hit in public_hits}
    assert CASE_RESTRICTED not in {hit.case_id for hit in public_hits}


def test_staff_access_and_structured_filters_are_applied_before_ranking(
    lexical_index: LexicalIndex,
) -> None:
    filters = QueryFilters.create(
        years=(2024,), domains=("재정",), case_types=("qa",), access_level="staff"
    )
    hits = lexical_index.search("학교회계 이월금", filters=filters, limit=10)

    assert [hit.case_id for hit in hits] == [CASE_STAFF]
    assert all(hit.review_status == "search_approved" for hit in hits)


def test_query_plan_uses_fts_and_reports_no_restricted_candidates(
    lexical_index: LexicalIndex,
) -> None:
    plan = lexical_index.inspect_plan("학교회계 제12조")

    assert plan.uses_fts is True
    assert plan.full_table_scan is False
    assert plan.restricted_candidates == 0


def test_fts_query_syntax_is_not_injectable(lexical_index: LexicalIndex) -> None:
    assert lexical_index.search('" OR * NOT 학교회계', limit=10) == ()


def test_build_rejects_malformed_canonical_payload_without_value_leak(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    sentinel = "PRIVATE_CANONICAL_SENTINEL"
    with sqlite3.connect(canonical) as connection:
        connection.execute(
            "UPDATE cases SET payload_json=? WHERE case_id=?",
            (json.dumps({"case_id": sentinel}), CASE_PUBLIC),
        )

    with pytest.raises(LexicalError, match="canonical_source_invalid") as captured:
        build_lexical_index(canonical, target)

    rendered = " ".join(
        (
            str(captured.value),
            repr(captured.value),
            repr(captured.value.__cause__),
            repr(captured.value.__context__),
        )
    )
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_build_rejects_chunk_source_span_outside_its_parent_case(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    with sqlite3.connect(canonical) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM chunks WHERE case_id=?", (CASE_PUBLIC,)
            ).fetchone()[0]
        )
        payload["source_span_indexes"] = [99]
        connection.execute(
            "UPDATE chunks SET payload_json=? WHERE case_id=?",
            (json.dumps(payload), CASE_PUBLIC),
        )

    with pytest.raises(LexicalError, match="canonical_source_invalid"):
        build_lexical_index(canonical, target)


def test_build_rejects_law_reference_not_declared_by_parent_case(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    with sqlite3.connect(canonical) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM cases WHERE case_id=?", (CASE_PUBLIC,)
            ).fetchone()[0]
        )
        payload["law_ref_ids"] = []
        connection.execute(
            "UPDATE cases SET payload_json=? WHERE case_id=?",
            (json.dumps(payload), CASE_PUBLIC),
        )

    with pytest.raises(LexicalError, match="canonical_source_invalid"):
        build_lexical_index(canonical, target)


def test_index_target_must_not_already_exist(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    target.write_bytes(b"occupied")

    with pytest.raises(LexicalError, match="index_target_exists"):
        build_lexical_index(canonical, target)


def test_index_publish_never_clobbers_a_concurrent_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "canonical.sqlite3"
    target = tmp_path / "lexical.sqlite3"
    _write_canonical_database(canonical)
    real_link = os.link

    def competing_link(
        source: str | Path,
        destination: str | Path,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        Path(destination).write_bytes(b"competing-index")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", competing_link)

    with pytest.raises(LexicalError, match="index_build_failed"):
        build_lexical_index(canonical, target)

    assert target.read_bytes() == b"competing-index"


def test_index_validation_reads_only_a_bounded_sqlite_header(
    lexical_index: LexicalIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_sizes: list[int] = []
    real_read = os.read

    def recording_read(descriptor: int, size: int) -> bytes:
        requested_sizes.append(size)
        return real_read(descriptor, size)

    monkeypatch.setattr(os, "read", recording_read)

    stable_path = lexical_module._database_path(lexical_index.path)
    assert stable_path == lexical_index.path.resolve()
    assert requested_sizes
    assert max(requested_sizes) <= 4_096


def test_index_and_config_symlink_leaves_are_rejected(
    lexical_index: LexicalIndex, tmp_path: Path
) -> None:
    index_link = tmp_path / "index-link.sqlite3"
    config_link = tmp_path / "retrieval-link.toml"
    index_link.symlink_to(lexical_index.path)
    config_link.symlink_to(
        Path(lexical_module.__file__).parents[2] / "config/retrieval.toml"
    )

    with pytest.raises(LexicalError, match="index_invalid"):
        LexicalIndex(index_link)
    with pytest.raises(LexicalError, match="config_invalid"):
        LexicalIndex(lexical_index.path, config_path=config_link)
