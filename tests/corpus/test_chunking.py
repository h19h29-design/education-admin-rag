"""Behavior contracts for locked tokenization and role-aware chunking."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import traceback
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.corpus.chunking import (
    BGE_M3_REQUIRED_PATHS,
    TOKENIZER_REQUIRED_PATHS,
    ChunkingError,
    EmbeddingModelLock,
    LockedEmbeddingFile,
    LockedTokenizer,
    RoleSource,
    TokenizerContract,
    VerifiedChunkSet,
    build_chunks,
    load_embedding_model_lock,
    load_locked_tokenizer,
    revalidate_verified_chunk_set,
    role_source_manifest_bytes,
    tokenizer_runtime_fingerprint_sha256,
    validate_embedding_model_lock,
    verify_embedding_cache,
    verify_role_sources,
)
from src.corpus.models import Case, SourceSpan

_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


def _cache_bytes(path: str) -> bytes:
    return f"fixture:{path}".encode()


def _embedding_file(path: str) -> dict[str, object]:
    payload = _cache_bytes(path)
    return {
        "path": path,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
        "source_url": f"https://huggingface.co/BAAI/bge-m3/resolve/{_REVISION}/{path}",
    }


def _composite_lock_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "language": "korean",
        "packages": {"paddleocr": "3.7.0", "paddlepaddle": "3.1.1"},
        "models": [{"opaque_ocr_slice": True}],
        "embedding_models": [
            {
                "repo_id": "BAAI/bge-m3",
                "revision": _REVISION,
                "files": [_embedding_file(path) for path in BGE_M3_REQUIRED_PATHS],
            }
        ],
    }


def _write_cache(root: Path, paths: tuple[str, ...]) -> None:
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_cache_bytes(path))


def test_checked_in_bge_m3_lock_is_official_immutable_and_complete() -> None:
    """Catches a mutable or incomplete checked-in dense runtime closure."""
    lock = load_embedding_model_lock(Path("config/models.lock.json"))
    expected_files = {
        "1_Pooling/config.json": (
            "e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352",
            191,
        ),
        "config.json": (
            "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
            687,
        ),
        "config_sentence_transformers.json": (
            "1eef72430e7194a1e59680e635aed81ffa083f05668dbc5bb1c56c04c0999c38",
            123,
        ),
        "modules.json": (
            "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
            349,
        ),
        "pytorch_model.bin": (
            "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
            2_271_145_830,
        ),
        "sentence_bert_config.json": (
            "eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1",
            54,
        ),
        "sentencepiece.bpe.model": (
            "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            5_069_051,
        ),
        "special_tokens_map.json": (
            "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
            964,
        ),
        "tokenizer.json": (
            "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
            17_098_108,
        ),
        "tokenizer_config.json": (
            "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4",
            444,
        ),
    }

    assert lock.repo_id == "BAAI/bge-m3"
    assert lock.revision == _REVISION
    assert {
        item.path: (item.sha256, item.size) for item in lock.files
    } == expected_files
    assert tuple(item.path for item in lock.files) == BGE_M3_REQUIRED_PATHS
    assert all(
        item.source_url
        == f"https://huggingface.co/BAAI/bge-m3/resolve/{_REVISION}/{item.path}"
        for item in lock.files
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda payload: payload["embedding_models"][0].update(revision="main"),
            "revision",
        ),
        (
            lambda payload: payload["embedding_models"][0].update(
                repo_id="attacker/bge-m3"
            ),
            "official",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"][0].update(
                source_url="https://huggingface.co/BAAI/bge-m3/resolve/main/config.json"
            ),
            "source URL",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"][0].update(
                path="../escape"
            ),
            "path",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"][0].update(
                sha256="A" * 64
            ),
            "SHA-256",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"][0].update(
                size=True
            ),
            "size",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"].pop(),
            "required files",
        ),
        (
            lambda payload: payload["embedding_models"][0]["files"].append(
                {
                    **_embedding_file("README.md"),
                }
            ),
            "required files",
        ),
        (
            lambda payload: payload.update(unreviewed_key=True),
            "top-level",
        ),
    ],
)
def test_embedding_lock_rejects_mutable_or_unreviewed_metadata(
    mutation: object, message: str
) -> None:
    """Catches trust metadata accepting mutable refs, weak hashes, or drift."""
    payload = _composite_lock_payload()
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(ChunkingError, match=message):
        validate_embedding_model_lock(payload)


def test_embedding_lock_rejects_nonexact_container_types() -> None:
    """Catches mapping subclasses presenting inconsistent trust metadata views."""

    class UntrustedDictionary(dict[str, object]):
        pass

    payload = UntrustedDictionary(_composite_lock_payload())

    with pytest.raises(ChunkingError, match="top-level"):
        validate_embedding_model_lock(payload)


def test_tokenizer_cache_gate_uses_exact_subset_without_model_binary(
    tmp_path: Path,
) -> None:
    """Catches corpus builds silently depending on the 2.27GB encoder checkpoint."""
    lock = validate_embedding_model_lock(_composite_lock_payload())
    _write_cache(tmp_path, TOKENIZER_REQUIRED_PATHS)

    verify_embedding_cache(
        lock,
        tmp_path,
        scope="tokenizer",
        expected_lock_sha256=lock.fingerprint_sha256,
    )

    with pytest.raises(ChunkingError, match="cache file set"):
        verify_embedding_cache(
            lock,
            tmp_path,
            scope="full",
            expected_lock_sha256=lock.fingerprint_sha256,
        )


def test_locked_tokenizer_loads_verified_json_bytes_without_path_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = validate_embedding_model_lock(_composite_lock_payload())
    _write_cache(tmp_path, TOKENIZER_REQUIRED_PATHS)

    class Encoding:
        tokens = ("fixture-token",)
        offsets = ((0, 7),)

    class Backend:
        def encode(self, text: str, *, add_special_tokens: bool) -> Encoding:
            assert text == "fixture"
            assert not add_special_tokens
            return Encoding()

        def token_to_id(self, token: str) -> int | None:
            return 1 if token == "fixture-token" else None

        def decode(self, ids: list[int], *, skip_special_tokens: bool) -> str:
            assert ids == [1]
            assert not skip_special_tokens
            return "fixture"

    class TokenizerFactory:
        @staticmethod
        def from_str(raw: str) -> Backend:
            assert raw == _cache_bytes("tokenizer.json").decode()
            return Backend()

    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        SimpleNamespace(Tokenizer=TokenizerFactory),
    )

    tokenizer = load_locked_tokenizer(
        lock,
        tmp_path,
        expected_lock_sha256=lock.fingerprint_sha256,
        runtime_fingerprint_sha256="d" * 64,
    )

    assert type(tokenizer) is LockedTokenizer
    assert tokenizer.tokenize("fixture") == ("fixture-token",)
    assert tokenizer.token_offsets("fixture") == ((0, 7),)
    assert tokenizer.detokenize(("fixture-token",)) == "fixture"
    assert tokenizer.model_name == "BAAI/bge-m3"
    assert tokenizer.revision == _REVISION


def test_tokenizer_runtime_fingerprint_binds_lock_bytes_and_image_digest() -> None:
    first = tokenizer_runtime_fingerprint_sha256(
        b"lock-v1\n", indexer_image_digest="sha256:" + "a" * 64
    )
    second = tokenizer_runtime_fingerprint_sha256(
        b"lock-v2\n", indexer_image_digest="sha256:" + "a" * 64
    )
    third = tokenizer_runtime_fingerprint_sha256(
        b"lock-v1\n", indexer_image_digest="sha256:" + "b" * 64
    )

    assert len(first) == 64
    assert len({first, second, third}) == 3


def test_embedding_cache_rejects_missing_extra_symlink_size_and_hash(
    tmp_path: Path,
) -> None:
    """Catches an incomplete, redirected, or modified offline model cache."""
    lock = validate_embedding_model_lock(_composite_lock_payload())
    cache = tmp_path / "cache"
    _write_cache(cache, BGE_M3_REQUIRED_PATHS)
    verify_embedding_cache(
        lock,
        cache,
        scope="full",
        expected_lock_sha256=lock.fingerprint_sha256,
    )

    (cache / "README.md").write_text("unlocked", encoding="utf-8")
    with pytest.raises(ChunkingError, match="cache file set"):
        verify_embedding_cache(
            lock,
            cache,
            scope="full",
            expected_lock_sha256=lock.fingerprint_sha256,
        )
    (cache / "README.md").unlink()

    (cache / "tokenizer.json").write_bytes(b"tampered")
    with pytest.raises(ChunkingError, match="size"):
        verify_embedding_cache(
            lock,
            cache,
            scope="full",
            expected_lock_sha256=lock.fingerprint_sha256,
        )
    (cache / "tokenizer.json").write_bytes(_cache_bytes("tokenizer.json"))

    original = (cache / "tokenizer.json").read_bytes()
    same_length_tamper = bytes([original[0] ^ 1]) + original[1:]
    (cache / "tokenizer.json").write_bytes(same_length_tamper)
    with pytest.raises(ChunkingError, match="SHA-256"):
        verify_embedding_cache(
            lock,
            cache,
            scope="full",
            expected_lock_sha256=lock.fingerprint_sha256,
        )
    (cache / "tokenizer.json").write_bytes(original)

    target = cache / "special_tokens_map.json"
    target.unlink()
    target.symlink_to(cache / "tokenizer_config.json")
    with pytest.raises(ChunkingError, match="cache"):
        verify_embedding_cache(
            lock,
            cache,
            scope="full",
            expected_lock_sha256=lock.fingerprint_sha256,
        )


def test_cache_gate_rejects_forged_lock_even_when_attacker_files_match(
    tmp_path: Path,
) -> None:
    """Catches caller-constructed metadata replacing the pinned file authority."""
    trusted = validate_embedding_model_lock(_composite_lock_payload())
    forged_files = tuple(
        LockedEmbeddingFile(
            path=item.path,
            sha256=hashlib.sha256(b"x").hexdigest(),
            size=1,
            source_url=item.source_url,
        )
        for item in trusted.files
    )
    forged = EmbeddingModelLock(
        repo_id=trusted.repo_id,
        revision=trusted.revision,
        files=forged_files,
    )
    for path in BGE_M3_REQUIRED_PATHS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")

    with pytest.raises(ChunkingError, match="pinned lock fingerprint"):
        verify_embedding_cache(
            forged,
            tmp_path,
            scope="full",
            expected_lock_sha256=trusted.fingerprint_sha256,
        )


def test_cache_gate_rejects_invalid_runtime_scope(tmp_path: Path) -> None:
    """Catches a misspelled gate scope silently falling back to the full closure."""
    lock = validate_embedding_model_lock(_composite_lock_payload())
    _write_cache(tmp_path, BGE_M3_REQUIRED_PATHS)

    with pytest.raises(ChunkingError, match="scope"):
        verify_embedding_cache(
            lock,
            tmp_path,
            scope="typo",  # type: ignore[arg-type]
            expected_lock_sha256=lock.fingerprint_sha256,
        )


def test_cache_root_parent_swap_cannot_redirect_verified_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an ancestor ABA swap between pathname checks and root open."""
    lock = validate_embedding_model_lock(_composite_lock_payload())
    holder = tmp_path / "holder"
    original_parent = holder / "trusted"
    cache = original_parent / "cache"
    cache.mkdir(parents=True)
    (cache / "unexpected").write_bytes(b"not-the-locked-cache")
    evil_parent = tmp_path / "evil"
    evil_cache = evil_parent / "cache"
    _write_cache(evil_cache, TOKENIZER_REQUIRED_PATHS)
    real_lstat = os.lstat
    swapped = False

    def swap_after_last_precheck(
        path: object, *args: object, **kwargs: object
    ) -> os.stat_result:
        nonlocal swapped
        result = real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]
        if not swapped and Path(path) == cache:
            swapped = True
            original_parent.rename(holder / "trusted-original")
            original_parent.symlink_to(evil_parent, target_is_directory=True)
        return result

    monkeypatch.setattr(os, "lstat", swap_after_last_precheck)

    with pytest.raises(ChunkingError, match="cache"):
        verify_embedding_cache(
            lock,
            cache,
            scope="tokenizer",
            expected_lock_sha256=lock.fingerprint_sha256,
        )


def test_embedding_lock_loader_is_canonical_bounded_and_value_free(
    tmp_path: Path,
) -> None:
    """Catches duplicate JSON keys or private bytes escaping the lock boundary."""
    sentinel = "PRIVATE-MODEL-LOCK-VALUE"
    lock_path = tmp_path / "models.lock.json"
    lock_path.write_text(
        json.dumps(_composite_lock_payload())[:-1]
        + f',"embedding_models":"{sentinel}","embedding_models":[]}}',
        encoding="utf-8",
    )

    with pytest.raises(ChunkingError, match="cannot load model lock") as captured:
        load_embedding_model_lock(lock_path)

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("kind", ["oversize", "directory", "symlink", "fifo"])
def test_embedding_lock_loader_rejects_nonregular_or_unbounded_input_without_hanging(
    tmp_path: Path, kind: str
) -> None:
    """Catches special files or oversized metadata causing unbounded lock reads."""
    target = tmp_path / "models.lock.json"
    if kind == "oversize":
        target.write_bytes(b"x" * (1_048_576 + 1))
    elif kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        source = tmp_path / "source.json"
        source.write_text("{}", encoding="utf-8")
        target.symlink_to(source)
    else:
        os.mkfifo(target)

    with pytest.raises(ChunkingError, match="cannot load model lock") as captured:
        load_embedding_model_lock(target)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class FakeTokenizer:
    """A deterministic fake implementing the locked tokenizer boundary."""

    def __init__(self, contract: TokenizerContract) -> None:
        self.model_name = contract.model_name
        self.revision = contract.revision
        self.model_lock_sha256 = contract.model_lock_sha256
        self.runtime_fingerprint_sha256 = contract.runtime_fingerprint_sha256

    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(text.split())

    def detokenize(self, tokens: tuple[str, ...]) -> str:
        return " ".join(tokens)

    def token_offsets(self, text: str) -> tuple[tuple[int, int], ...]:
        offsets: list[tuple[int, int]] = []
        cursor = 0
        for token in text.split():
            start = text.index(token, cursor)
            end = start + len(token)
            offsets.append((start, end))
            cursor = end
        return tuple(offsets)


def _contract() -> TokenizerContract:
    return TokenizerContract(
        model_name="BAAI/bge-m3",
        revision=_REVISION,
        model_lock_sha256="f" * 64,
        runtime_fingerprint_sha256="e" * 64,
    )


def _verified_sources(
    case: Case, sources: tuple[RoleSource, ...]
) -> tuple[object, str]:
    rendered = role_source_manifest_bytes(case, sources)
    fingerprint = hashlib.sha256(rendered).hexdigest()
    return (
        verify_role_sources(
            case,
            sources,
            expected_authority_sha256=fingerprint,
        ),
        fingerprint,
    )


def _build_test_chunks(
    case: Case,
    sources: tuple[RoleSource, ...],
    *,
    table_authority: dict[int, str] | None = None,
) -> VerifiedChunkSet:
    verified, fingerprint = _verified_sources(case, sources)
    contract = _contract()
    return build_chunks(
        case,
        verified,
        tokenizer=FakeTokenizer(contract),
        contract=contract,
        expected_role_authority_sha256=fingerprint,
        expected_table_evidence_sha256s=table_authority,
    )


def _source_span(raw_text: str, page: int, y0: float) -> SourceSpan:
    return SourceSpan(
        pdf_page_index=page,
        page_label=str(page),
        bbox=(10.0, y0, 500.0, y0 + 20.0),
        text_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
    )


def _approved_case(
    *,
    question: str,
    answer: str,
    raw_texts: tuple[str, ...],
    pages: tuple[int, ...],
    basis_text: str | None = None,
    pii_class: str = "none",
    search_eligible: bool = True,
    answer_eligible: bool = True,
) -> Case:
    spans = tuple(
        _source_span(raw, page, float(index * 30 + 10))
        for index, (raw, page) in enumerate(zip(raw_texts, pages, strict=True))
    )
    return Case(
        case_id="senqa-2025-contract-contract-general-1",
        legacy_ids=(),
        doc_id="sen-qa-2025",
        case_type="qa",
        domain="계약",
        part="계약 일반",
        subtopic="입찰",
        case_no="1",
        title_raw="2단계 입찰",
        title_normalized="2단계 입찰",
        question=question,
        answer=answer,
        facts=None,
        basis_text=basis_text,
        law_ref_ids=(),
        source_spans=spans,
        extraction_source="ocr",
        extraction_confidence=0.99,
        critical_field_review="verified",
        pii_class=pii_class,
        anonymization_status="not_required",
        currency_status="historical_reference",
        search_eligible=search_eligible,
        answer_eligible=answer_eligible,
        review_status="approved",
    )


def test_chunks_never_cross_case_or_page_boundaries() -> None:
    """Catches role text being attached to an unrelated case or source page."""
    question = "입찰 평가위원회 구성 근거를 알려 주세요"
    answer = "위원회 구성은 해당 지침과 내부 절차를 따라야 합니다"
    basis = "지방계약법 제12조를 참고합니다"
    raw_texts = (f"질문: {question}", f"답변: {answer}", f"근거: {basis}")
    case = _approved_case(
        question=question,
        answer=answer,
        basis_text=basis,
        raw_texts=raw_texts,
        pages=(13, 14, 14),
    )
    sources = (
        RoleSource("question", question, raw_texts[0], 0),
        RoleSource("answer", answer, raw_texts[1], 1),
        RoleSource("basis", basis, raw_texts[2], 2),
    )
    chunks = _build_test_chunks(case, sources)

    assert {chunk.case_id for chunk in chunks} == {case.case_id}
    assert {chunk.role for chunk in chunks} >= {"question", "answer", "basis"}
    assert all(
        len(
            {
                case.source_spans[index].pdf_page_index
                for index in chunk.source_span_indexes
            }
        )
        == 1
        for chunk in chunks
    )
    assert all(
        chunk.token_count == len(chunk.embedding_text.split()) for chunk in chunks
    )


def test_long_fragment_overlap_stays_inside_one_source_span() -> None:
    """Catches overlap crossing a role, page, or source span to meet size targets."""
    question = " ".join(f"q{index:03d}" for index in range(90))
    answer = " ".join(f"a{index:03d}" for index in range(900))
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    sources = (
        RoleSource("question", question, question, 0),
        RoleSource("answer", answer, answer, 1),
    )
    chunks = _build_test_chunks(case, sources)
    answers = [chunk for chunk in chunks if chunk.role == "answer"]

    assert len(answers) >= 3
    assert all(chunk.token_count <= 450 for chunk in answers)
    assert all(chunk.source_span_indexes == (1,) for chunk in answers)
    for left, right in pairwise(answers):
        left_tokens = left.text.split()
        right_tokens = right.text.split()
        overlap = 0
        for count in range(1, min(len(left_tokens), len(right_tokens)) + 1):
            if left_tokens[-count:] == right_tokens[:count]:
                overlap = count
        assert 0.10 <= overlap / len(left_tokens) <= 0.15


def test_typed_table_rows_repeat_headers_without_inference() -> None:
    """Catches pipe/newline heuristics fabricating table structure or losing headers."""
    question = "계약 기준은 무엇인가요"
    answer = "표의 기준을 적용합니다"
    table_row = "1단계 | 5천만원 이하"
    header_raw = "단계 | 기준금액"
    raw_texts = (question, answer, header_raw, table_row)
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=raw_texts,
        pages=(13, 13, 13, 13),
    )
    table_evidence = "d" * 64
    chunks = _build_test_chunks(
        case,
        (
            RoleSource("question", question, question, 0),
            RoleSource("answer", answer, answer, 1),
            RoleSource(
                "table",
                table_row,
                table_row,
                3,
                table_header="단계 | 기준금액",
                table_header_raw_text=header_raw,
                table_header_source_span_index=2,
                table_evidence_sha256=table_evidence,
            ),
        ),
        table_authority={3: table_evidence},
    )

    table = next(chunk for chunk in chunks if chunk.role == "table")
    assert table.text == "단계 | 기준금액\n1단계 | 5천만원 이하"


def test_table_authority_requires_exact_source_key_set() -> None:
    """Catches unrelated authority entries being silently carried into a build."""
    question = "계약 기준은 무엇인가요"
    answer = "표의 기준을 적용합니다"
    header = "단계 | 기준금액"
    row = "1단계 | 5천만원 이하"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer, header, row),
        pages=(13, 13, 13, 13),
    )
    evidence = "d" * 64
    sources = (
        RoleSource("question", question, question, 0),
        RoleSource("answer", answer, answer, 1),
        RoleSource(
            "table",
            row,
            row,
            3,
            table_header=header,
            table_header_raw_text=header,
            table_header_source_span_index=2,
            table_evidence_sha256=evidence,
        ),
    )
    verified, fingerprint = _verified_sources(case, sources)
    contract = _contract()

    with pytest.raises(ChunkingError, match="table role authority"):
        build_chunks(
            case,
            verified,
            tokenizer=FakeTokenizer(contract),
            contract=contract,
            expected_role_authority_sha256=fingerprint,
            expected_table_evidence_sha256s={3: evidence, 99: "e" * 64},
        )


@pytest.mark.parametrize("failure", ["raw-hash", "aggregate", "span-index"])
def test_chunking_rejects_unbound_role_sources_without_values(failure: str) -> None:
    """Catches normalized text or a forged span entering canonical provenance."""
    sentinel = "PRIVATE-SOURCE-BODY"
    question = "질문 본문"
    answer = "답변 본문"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    question_source = RoleSource("question", question, question, 0)
    if failure == "raw-hash":
        question_source = RoleSource("question", question, sentinel, 0)
    elif failure == "aggregate":
        question_source = RoleSource("question", sentinel, question, 0)
    elif failure == "span-index":
        question_source = RoleSource("question", question, question, 99)
    with pytest.raises(ChunkingError, match="role source") as captured:
        role_source_manifest_bytes(
            case,
            (question_source, RoleSource("answer", answer, answer, 1)),
        )

    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_chunking_rejects_untrusted_tokenizer_identity() -> None:
    """Catches chunk counts being produced by a tokenizer outside the pinned revision."""
    question = "질문 본문"
    answer = "답변 본문"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    contract = _contract()
    tokenizer = FakeTokenizer(contract)
    tokenizer.revision = "0" * 40
    sources = (
        RoleSource("question", question, question, 0),
        RoleSource("answer", answer, answer, 1),
    )
    verified, fingerprint = _verified_sources(case, sources)

    with pytest.raises(ChunkingError, match="tokenizer identity"):
        build_chunks(
            case,
            verified,
            tokenizer=tokenizer,
            contract=contract,
            expected_role_authority_sha256=fingerprint,
        )


def test_multiple_answer_fragments_remain_on_their_own_source_pages() -> None:
    """Catches target-size aggregation joining role fragments across pages."""
    question = "질문 본문"
    first = "첫 번째 페이지 답변"
    second = "두 번째 페이지 답변"
    answer = f"{first}\n{second}"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, first, second),
        pages=(13, 14, 15),
    )

    chunks = _build_test_chunks(
        case,
        (
            RoleSource("question", question, question, 0),
            RoleSource("answer", first, first, 1),
            RoleSource("answer", second, second, 2),
        ),
    )
    answers = [chunk for chunk in chunks if chunk.role == "answer"]

    assert [chunk.text for chunk in answers] == [first, second]
    assert [chunk.source_span_indexes for chunk in answers] == [(1,), (2,)]


def test_adjacent_same_page_fragments_group_without_cross_span_overlap() -> None:
    """Catches parser line fragments becoming unusably tiny independent chunks."""
    question = "질문 본문"
    first = " ".join(f"첫번째{index:03d}" for index in range(130))
    second = " ".join(f"둘째{index:03d}" for index in range(130))
    answer = f"{first}\n{second}"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, first, second),
        pages=(13, 14, 14),
    )

    chunks = _build_test_chunks(
        case,
        (
            RoleSource("question", question, question, 0),
            RoleSource("answer", first, first, 1),
            RoleSource("answer", second, second, 2),
        ),
    )
    answers = [chunk for chunk in chunks if chunk.role == "answer"]

    assert [chunk.text for chunk in answers] == [answer]
    assert answers[0].source_span_indexes == (1, 2)
    assert 250 <= answers[0].token_count <= 450


def test_long_split_preserves_exact_source_characters() -> None:
    """Catches tokenizer decode normalizing whitespace or Unicode in chunk text."""
    question = "질문 본문"
    answer = "  ".join(f"법령･지침{index:03d}" for index in range(700))
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )

    chunks = _build_test_chunks(
        case,
        (
            RoleSource("question", question, question, 0),
            RoleSource("answer", answer, answer, 1),
        ),
    )

    assert all(chunk.text in answer for chunk in chunks if chunk.role == "answer")
    assert all("･" in chunk.text for chunk in chunks if chunk.role == "answer")


def test_self_consistent_forged_role_authority_cannot_replace_external_pin() -> None:
    """Catches a forged Case and role text sharing an unrelated valid raw span."""
    raw_question = "실제 원문 질문"
    answer = "실제 원문 답변"
    original = _approved_case(
        question=raw_question,
        answer=answer,
        raw_texts=(raw_question, answer),
        pages=(13, 14),
    )
    original_sources = (
        RoleSource("question", raw_question, raw_question, 0),
        RoleSource("answer", answer, answer, 1),
    )
    external_pin = hashlib.sha256(
        role_source_manifest_bytes(original, original_sources)
    ).hexdigest()
    forged_question = "검수되지 않은 질문"
    forged = original.model_copy(update={"question": forged_question})
    forged_sources = (
        RoleSource("question", forged_question, raw_question, 0),
        RoleSource("answer", answer, answer, 1),
    )

    with pytest.raises(ChunkingError, match="external role authority"):
        verify_role_sources(
            forged,
            forged_sources,
            expected_authority_sha256=external_pin,
        )


def test_self_consistent_forged_chunk_set_cannot_replace_external_pin() -> None:
    """Catches recomputing the public binding after mutating canonical chunk bytes."""
    question = "질문 본문"
    answer = "답변 본문"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    verified_sources, role_fingerprint = _verified_sources(
        case,
        (
            RoleSource("question", question, question, 0),
            RoleSource("answer", answer, answer, 1),
        ),
    )
    contract = _contract()
    original = build_chunks(
        case,
        verified_sources,
        tokenizer=FakeTokenizer(contract),
        contract=contract,
        expected_role_authority_sha256=role_fingerprint,
    )
    first = original.chunks[0].model_copy(
        update={
            "text": "검수되지 않은 본문",
            "embedding_text": original.chunks[0].embedding_text.rsplit("\n", 1)[0]
            + "\n검수되지 않은 본문",
        }
    )
    forged_chunks = (first, *original.chunks[1:])
    payload = {
        "case_content_sha256": original.case_content_sha256,
        "chunks": [chunk.model_dump(mode="json") for chunk in forged_chunks],
        "role_authority_sha256": original.role_authority_sha256,
        "table_authorities": [],
        "tokenizer_contract": {
            "model_lock_sha256": contract.model_lock_sha256,
            "model_name": contract.model_name,
            "revision": contract.revision,
            "runtime_fingerprint_sha256": contract.runtime_fingerprint_sha256,
        },
    }
    forged_binding = hashlib.sha256(
        b"sen-qa-verified-chunk-set-v1\0"
        + json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    forged = object.__new__(VerifiedChunkSet)
    object.__setattr__(forged, "chunks", forged_chunks)
    object.__setattr__(forged, "case_content_sha256", original.case_content_sha256)
    object.__setattr__(forged, "role_authority_sha256", original.role_authority_sha256)
    object.__setattr__(forged, "table_authorities", ())
    object.__setattr__(forged, "tokenizer_contract", contract)
    object.__setattr__(forged, "binding_sha256", forged_binding)

    with pytest.raises(ChunkingError, match="binding"):
        revalidate_verified_chunk_set(
            forged,
            case,
            contract=contract,
            expected_role_authority_sha256=role_fingerprint,
            expected_chunk_set_sha256=original.binding_sha256,
        )


def test_malformed_exact_chunking_dataclasses_fail_value_free(tmp_path: Path) -> None:
    """Catches annotation bypass producing TypeError or value-bearing diagnostics."""
    sentinel = "PRIVATE-CHUNK-DATACLASS"
    lock = validate_embedding_model_lock(_composite_lock_payload())
    object.__setattr__(lock, "files", 7)

    with pytest.raises(ChunkingError, match="model lock") as captured:
        verify_embedding_cache(
            lock,
            tmp_path,
            scope="tokenizer",
            expected_lock_sha256="f" * 64,
        )

    rendered = "".join(traceback.format_exception(captured.value))
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_tokenizer_failures_do_not_retain_private_source_values() -> None:
    """Catches tokenizer exceptions remaining reachable through error context."""
    sentinel = "PRIVATE-TOKENIZER-SOURCE"
    question = "질문 본문"
    answer = "답변 본문"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    sources = (
        RoleSource("question", question, question, 0),
        RoleSource("answer", answer, answer, 1),
    )
    verified, fingerprint = _verified_sources(case, sources)
    contract = _contract()

    class FailingTokenizer(FakeTokenizer):
        def tokenize(self, text: str) -> tuple[str, ...]:
            raise RuntimeError(sentinel)

    with pytest.raises(ChunkingError, match="tokenizer failed") as captured:
        build_chunks(
            case,
            verified,
            tokenizer=FailingTokenizer(contract),
            contract=contract,
            expected_role_authority_sha256=fingerprint,
        )

    rendered = "".join(traceback.format_exception(captured.value))
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_tokenizer_identity_exception_is_value_free() -> None:
    """Catches hostile tokenizer attributes escaping the public boundary."""
    sentinel = "PRIVATE-TOKENIZER-IDENTITY"
    question = "질문 본문"
    answer = "답변 본문"
    case = _approved_case(
        question=question,
        answer=answer,
        raw_texts=(question, answer),
        pages=(13, 14),
    )
    sources = (
        RoleSource("question", question, question, 0),
        RoleSource("answer", answer, answer, 1),
    )
    verified, fingerprint = _verified_sources(case, sources)
    contract = _contract()

    class HostileTokenizer:
        @property
        def model_name(self) -> str:
            raise RuntimeError(sentinel)

    with pytest.raises(ChunkingError, match="tokenizer identity") as captured:
        build_chunks(
            case,
            verified,
            tokenizer=HostileTokenizer(),  # type: ignore[arg-type]
            contract=contract,
            expected_role_authority_sha256=fingerprint,
        )

    rendered = "".join(traceback.format_exception(captured.value))
    assert sentinel not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
