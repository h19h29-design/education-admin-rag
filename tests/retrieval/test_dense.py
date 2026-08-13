from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

import src.cli as cli_module
import src.retrieval.dense as dense_module
from src.cli import app
from src.corpus.chunking import ChunkingError, load_embedding_model_lock
from src.corpus.models import Case, Chunk, Document
from src.retrieval.dense import (
    DenseEncoder,
    DenseError,
    DenseIndex,
    DensePoint,
    DenseSearchFilters,
    build_dense_candidate,
    create_qdrant_client,
    export_dense_snapshot,
)
from tests.retrieval.test_lexical import (
    CASE_PUBLIC,
    _case,
    _chunk,
    _document,
    _write_canonical_database,
)

LOCK_PATH = Path("config/models.lock.json")
RELEASE_ID = "corpus-20250808123456-deadbeef"


class FakeBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
        self.calls.append({"texts": texts, **kwargs})
        if self.fail:
            raise MemoryError("PRIVATE_MODEL_SENTINEL")
        return [[3.0, 4.0] for _ in texts]


class FakeQdrant:
    def __init__(self) -> None:
        self.collections: dict[str, list[object]] = {}
        self.vector_sizes: dict[str, int] = {}
        self.vector_distances: dict[str, object] = {}
        self.alias_operations: list[object] = []
        self.alias_targets: dict[str, str] = {}
        self.last_filter: object | None = None
        self.query_response: tuple[object, ...] = ()
        self.upsert_status: object = "completed"

    def collection_exists(self, collection_name: str, **_: object) -> bool:
        return collection_name in self.collections

    def create_collection(
        self, collection_name: str, vectors_config: object, **_: object
    ) -> bool:
        self.collections[collection_name] = []
        self.vector_sizes[collection_name] = int(cast(Any, vectors_config).size)
        self.vector_distances[collection_name] = cast(Any, vectors_config).distance
        return True

    def get_collection(self, collection_name: str, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(
                        size=self.vector_sizes[collection_name],
                        distance=self.vector_distances[collection_name],
                    )
                )
            )
        )

    def upsert(
        self, collection_name: str, points: list[object], **_: object
    ) -> SimpleNamespace:
        self.collections[collection_name].extend(points)
        return SimpleNamespace(status=self.upsert_status)

    def count(self, collection_name: str, **_: object) -> SimpleNamespace:
        return SimpleNamespace(count=len(self.collections[collection_name]))

    def retrieve(
        self, collection_name: str, ids: list[object], **_: object
    ) -> list[object]:
        wanted = set(ids)
        return [
            SimpleNamespace(
                id=cast(Any, point).id,
                vector=cast(Any, point).vector,
                payload=cast(Any, point).payload,
            )
            for point in self.collections[collection_name]
            if cast(Any, point).id in wanted
        ]

    def query_points(
        self, collection_name: str, *, query_filter: object, **_: object
    ) -> SimpleNamespace:
        self.last_filter = query_filter
        return SimpleNamespace(points=self.query_response)

    def update_collection_aliases(self, operations: list[object], **_: object) -> bool:
        self.alias_operations.extend(operations)
        return True

    def get_aliases(self, **_: object) -> SimpleNamespace:
        return SimpleNamespace(
            aliases=tuple(
                SimpleNamespace(alias_name=alias, collection_name=collection)
                for alias, collection in sorted(self.alias_targets.items())
            )
        )


def _valid_models() -> tuple[Document, Case, Chunk]:
    document = _document(year=2025, access_level="public")
    case = _case(
        CASE_PUBLIC,
        document=document,
        domain="계약",
        title="2단계 입찰",
        question="지방계약법 제12조의 기준은 무엇인가요?",
    )
    return document, case, _chunk(case)


def _point(*, status: str = "approved", pii_class: str = "none") -> DensePoint:
    document = _document(year=2025, access_level="public")
    case = _case(
        CASE_PUBLIC,
        document=document,
        domain="계약",
        title="2단계 입찰",
        question="지방계약법 제12조의 기준은 무엇인가요?",
        status=status,
        pii_class=pii_class,
    )
    return DensePoint.create(
        document=document,
        case=case,
        chunk=_chunk(case),
        vector=(0.6, 0.8),
        corpus_version=RELEASE_ID,
        embedding_version="bge-m3-5617a9f6",
    )


def test_dense_encoder_rejects_mutable_revision_before_cache_or_model_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    payload["embedding_models"][0]["revision"] = "main"
    lock_path = tmp_path / "models.lock.json"
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    called = False

    def forbidden_loader(_: Path) -> object:
        nonlocal called
        called = True
        return object()

    monkeypatch.setattr(dense_module, "_load_sentence_transformer", forbidden_loader)

    with pytest.raises(DenseError, match="immutable_revision_required") as captured:
        DenseEncoder.from_lock(
            lock_path,
            model_root=tmp_path / "cache",
            expected_lock_sha256="0" * 64,
        )

    assert called is False
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dense_encoder_verifies_full_cache_and_normalizes_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = load_embedding_model_lock(LOCK_PATH)
    verified: list[tuple[object, Path, str, str]] = []
    backend = FakeBackend()

    def verify(
        lock_value: object, root: Path, *, scope: str, expected_lock_sha256: str
    ) -> None:
        verified.append((lock_value, root, scope, expected_lock_sha256))

    monkeypatch.setattr(dense_module, "verify_embedding_cache", verify)
    monkeypatch.setattr(dense_module, "_load_sentence_transformer", lambda _: backend)
    encoder = DenseEncoder.from_lock(
        LOCK_PATH,
        model_root=tmp_path / "cache",
        expected_lock_sha256=lock.fingerprint_sha256,
        batch_size=16,
    )

    vectors = encoder.encode(("학교회계", "제12조"))

    assert verified == [(lock, tmp_path / "cache", "full", lock.fingerprint_sha256)]
    assert vectors == ((0.6, 0.8), (0.6, 0.8))
    assert backend.calls == [
        {
            "texts": ["학교회계", "제12조"],
            "batch_size": 16,
            "convert_to_numpy": False,
            "normalize_embeddings": True,
            "show_progress_bar": False,
        }
    ]
    assert encoder.revision == lock.revision


def test_dense_encoder_builds_points_from_exact_chunk_embedding_text() -> None:
    lock = load_embedding_model_lock(LOCK_PATH)
    backend = FakeBackend()
    encoder = DenseEncoder(backend=backend, lock=lock, batch_size=16)
    document, case, chunk = _valid_models()

    points = encoder.build_points(((document, case, chunk),), corpus_version=RELEASE_ID)

    assert len(points) == 1
    assert points[0].vector == (0.6, 0.8)
    assert points[0].chunk_id == chunk.chunk_id
    assert points[0].embedding_version == encoder.embedding_version
    assert backend.calls[0]["texts"] == [chunk.embedding_text]


def test_dense_candidate_build_reads_canonical_storage_without_alias_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    _write_canonical_database(database)
    encoder = DenseEncoder(
        backend=FakeBackend(),
        lock=load_embedding_model_lock(LOCK_PATH),
        batch_size=16,
    )
    client = FakeQdrant()

    result = build_dense_candidate(
        database,
        client=client,
        encoder=encoder,
        release_id=RELEASE_ID,
    )

    assert result.point_count == 2
    assert result.collection_name == f"{RELEASE_ID}-bge-m3"
    assert len(result.sampled_vector_sha256) == 64
    assert client.alias_operations == []


def test_dense_encoder_filters_restricted_content_before_model_execution() -> None:
    lock = load_embedding_model_lock(LOCK_PATH)
    backend = FakeBackend()
    encoder = DenseEncoder(backend=backend, lock=lock, batch_size=16)
    document = _document(year=2025, access_level="public")
    restricted = _case(
        CASE_PUBLIC,
        document=document,
        domain="계약",
        title="제한 자료",
        question="PRIVATE_RESTRICTED_SENTINEL",
        pii_class="restricted",
    )
    chunk = _chunk(restricted)

    points = encoder.build_points(
        ((document, restricted, chunk),), corpus_version=RELEASE_ID
    )

    assert points == ()
    assert backend.calls == []


def test_dense_encoder_fails_closed_on_oom_without_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = load_embedding_model_lock(LOCK_PATH)
    monkeypatch.setattr(
        dense_module, "verify_embedding_cache", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        dense_module, "_load_sentence_transformer", lambda _: FakeBackend(fail=True)
    )
    encoder = DenseEncoder.from_lock(
        LOCK_PATH,
        model_root=tmp_path / "cache",
        expected_lock_sha256=lock.fingerprint_sha256,
    )

    with pytest.raises(DenseError, match="encoding_failed") as captured:
        encoder.encode(("PRIVATE_TEXT_SENTINEL",))

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


def test_dense_encoder_rejects_an_oversized_backend_vector() -> None:
    class OversizedBackend:
        def encode(self, texts: list[str], **kwargs: object) -> list[list[float]]:
            return [[1.0] * 4_097 for _ in texts]

    lock = load_embedding_model_lock(LOCK_PATH)
    encoder = DenseEncoder(backend=OversizedBackend(), lock=lock, batch_size=16)

    with pytest.raises(DenseError, match="encoding_failed") as captured:
        encoder.encode(("PRIVATE_VECTOR_SENTINEL",))

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dense_point_contains_required_parent_and_span_payload() -> None:
    point = _point()

    assert point.payload["chunk_id"] == f"chunk-{CASE_PUBLIC}"
    assert point.payload["case_id"] == CASE_PUBLIC
    assert point.payload["doc_id"] == "doc-2025-public"
    assert point.payload["edition_year"] == 2025
    assert point.payload["domain"] == "계약"
    assert point.payload["part"] == "계약"
    assert point.payload["case_type"] == "qa"
    assert point.payload["pdf_page_indexes"] == [13]
    assert point.payload["source_span_indexes"] == [0]
    assert point.payload["corpus_version"] == RELEASE_ID
    assert point.payload["embedding_version"] == "bge-m3-5617a9f6"


@pytest.mark.parametrize(
    "point",
    (
        pytest.param(_point(status="needs_review"), id="pending"),
        pytest.param(_point(pii_class="restricted"), id="restricted"),
    ),
)
def test_ineligible_or_restricted_point_is_never_upserted(point: DensePoint) -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )

    assert index.upsert((point,)) == 0
    assert client.collections[index.collection_name] == []


def test_dense_index_uses_versioned_collection_and_count_hash_gate() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()

    assert index.collection_name == f"{RELEASE_ID}-bge-m3"
    assert index.upsert((point,)) == 1
    result = index.verify(expected_eligible_count=1)

    assert result.point_count == 1
    assert len(result.sampled_vector_sha256) == 64
    assert result.collection_name == index.collection_name
    assert client.alias_operations == []


def test_candidate_verification_never_replaces_the_current_alias() -> None:
    client = FakeQdrant()
    client.alias_targets["education-admin-current"] = "corpus-old-bge-m3"
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    index.upsert((_point(),))

    index.verify(expected_eligible_count=1)

    assert client.alias_operations == []
    assert client.alias_targets == {"education-admin-current": "corpus-old-bge-m3"}


@pytest.mark.parametrize(
    ("vector_size", "distance"),
    (
        pytest.param(3, "Cosine", id="wrong-size"),
        pytest.param(2, "Dot", id="wrong-distance"),
    ),
)
def test_existing_collection_schema_must_match_the_dense_contract(
    vector_size: int, distance: str
) -> None:
    client = FakeQdrant()
    collection = f"{RELEASE_ID}-bge-m3"
    client.collections[collection] = []
    client.vector_sizes[collection] = vector_size
    client.vector_distances[collection] = distance
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )

    with pytest.raises(DenseError, match="collection_schema_mismatch"):
        index.upsert((_point(),))

    assert client.collections[collection] == []


def test_upsert_must_receive_a_completed_qdrant_result() -> None:
    client = FakeQdrant()
    client.upsert_status = "acknowledged"
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )

    with pytest.raises(DenseError, match="upsert_failed"):
        index.upsert((_point(),))


def test_count_mismatch_fails_without_alias_mutation() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    index.upsert((_point(),))

    with pytest.raises(DenseError, match="point_count_mismatch"):
        index.verify(expected_eligible_count=2)

    assert client.alias_operations == []


def test_remote_count_must_be_an_exact_integer() -> None:
    class StringCountQdrant(FakeQdrant):
        def count(self, collection_name: str, **kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(count="1")

    client = StringCountQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    index.upsert((_point(),))

    with pytest.raises(DenseError, match="verification_failed"):
        index.verify(expected_eligible_count=1)


def test_dense_search_always_builds_eligibility_status_and_access_filters() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    index.upsert((_point(),))
    filters = DenseSearchFilters.create(
        years=(2025,), domains=("계약",), case_types=("qa",), access_level="public"
    )

    assert index.search((0.6, 0.8), filters=filters, limit=10) == ()
    rendered_filter = cast(Any, client.last_filter).model_dump(mode="json")
    rendered = json.dumps(rendered_filter, sort_keys=True)
    assert "search_eligible" in rendered
    assert "review_status" in rendered
    assert "pii_class" in rendered
    assert "access_level" in rendered
    assert "edition_year" in rendered
    assert "domain" in rendered
    assert "case_type" in rendered


@pytest.mark.parametrize(
    ("payload_key", "payload_value"),
    (
        pytest.param("pii_class", "restricted", id="restricted"),
        pytest.param("review_status", "needs_review", id="unreviewed"),
        pytest.param("edition_year", 2024, id="wrong-year"),
        pytest.param("access_level", "staff", id="wrong-access"),
    ),
)
def test_search_revalidates_remote_payload_against_requested_policy(
    payload_key: str, payload_value: object
) -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    payload = {**point.payload, payload_key: payload_value}
    client.query_response = (
        SimpleNamespace(id=point.point_id, payload=payload, score=0.75),
    )
    filters = DenseSearchFilters.create(
        years=(2025,), domains=("계약",), case_types=("qa",), access_level="public"
    )

    with pytest.raises(DenseError, match="dense_search_failed") as captured:
        index.search((0.6, 0.8), filters=filters)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_search_rejects_more_remote_hits_than_the_requested_limit() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    hit = SimpleNamespace(id=point.point_id, payload=point.payload, score=0.75)
    client.query_response = (hit, hit)

    with pytest.raises(DenseError, match="dense_search_failed"):
        index.search((0.6, 0.8), filters=DenseSearchFilters.create(), limit=1)


def test_dense_search_has_no_filterless_call_shape() -> None:
    signature = inspect.signature(DenseIndex.search)

    assert signature.parameters["filters"].default is inspect.Parameter.empty


def test_mutated_dense_filter_is_rejected_with_a_fixed_error() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    malformed = replace(
        DenseSearchFilters.create(), years=cast(Any, ("PRIVATE_FILTER_SENTINEL",))
    )

    with pytest.raises(DenseError, match="dense_search_invalid") as captured:
        index.search((0.6, 0.8), filters=malformed)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_missing_qdrant_runtime_is_a_fixed_search_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )

    def missing_models() -> object:
        raise ImportError("PRIVATE_IMPORT_SENTINEL")

    monkeypatch.setattr(dense_module, "_qdrant_models", missing_models)
    with pytest.raises(DenseError, match="dense_search_failed") as captured:
        index.search((0.6, 0.8), filters=DenseSearchFilters.create())

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dense_point_rejects_non_normalized_vector() -> None:
    document, case, chunk = _valid_models()

    with pytest.raises(DenseError, match="dense_point_invalid"):
        DensePoint.create(
            document=document,
            case=case,
            chunk=chunk,
            vector=(3.0, 4.0),
            corpus_version=RELEASE_ID,
            embedding_version="bge-m3-5617a9f6",
        )


def test_sample_hash_is_stable_for_the_same_vector_and_point_identity() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    index.upsert((point,))

    first = index.verify(expected_eligible_count=1).sampled_vector_sha256
    second = index.verify(expected_eligible_count=1).sampled_vector_sha256

    assert first == second
    assert first != hashlib.sha256(b"").hexdigest()
    assert math.isclose(math.sqrt(sum(value * value for value in point.vector)), 1.0)


def test_remote_vector_tampering_fails_the_local_sample_hash_gate() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    index.upsert((point,))
    client.collections[index.collection_name] = [
        SimpleNamespace(id=point.point_id, vector=[1.0, 0.0], payload=point.payload)
    ]

    with pytest.raises(DenseError, match="sample_verification_failed"):
        index.verify(expected_eligible_count=1)


def test_remote_float32_overflow_is_a_fixed_sample_error() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    index.upsert((point,))
    client.collections[index.collection_name] = [
        SimpleNamespace(id=point.point_id, vector=[1e308, 0.0], payload=point.payload)
    ]

    with pytest.raises(DenseError, match="sample_verification_failed") as captured:
        index.verify(expected_eligible_count=1)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_remote_sample_identity_is_revalidated_before_hashing() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    point = _point()
    index.upsert((point,))
    client.collections[index.collection_name] = [
        SimpleNamespace(
            id="문자열-변조", vector=list(point.vector), payload=point.payload
        )
    ]

    with pytest.raises(DenseError, match="sample_verification_failed") as captured:
        index.verify(expected_eligible_count=1)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_remote_sample_collection_must_be_a_list() -> None:
    class InvalidRetrieveQdrant(FakeQdrant):
        def retrieve(
            self, collection_name: str, ids: list[object], **kwargs: object
        ) -> Any:
            return None

    client = InvalidRetrieveQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    index.upsert((_point(),))

    with pytest.raises(DenseError, match="verification_failed") as captured:
        index.verify(expected_eligible_count=1)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_malformed_dense_point_type_bomb_is_a_fixed_error() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    malformed = replace(_point(), review_status=cast(Any, []))

    with pytest.raises(DenseError, match="dense_points_invalid") as captured:
        index.upsert((malformed,))

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_malformed_dense_point_identity_type_bomb_is_a_fixed_error() -> None:
    client = FakeQdrant()
    index = DenseIndex(
        client,
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )
    malformed = replace(_point(), chunk_id=cast(Any, []))

    with pytest.raises(DenseError, match="dense_points_invalid") as captured:
        index.upsert((malformed,))

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_qdrant_client_in_memory_api_obeys_the_same_filtered_contract() -> None:
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    index = DenseIndex(
        cast(Any, client),
        release_id=RELEASE_ID,
        vector_size=2,
        embedding_version="bge-m3-5617a9f6",
    )

    assert index.upsert((_point(),)) == 1
    assert index.verify(expected_eligible_count=1).point_count == 1
    hits = index.search(
        (0.6, 0.8), filters=DenseSearchFilters.create(access_level="public"), limit=5
    )

    assert [hit.case_id for hit in hits] == [CASE_PUBLIC]


def test_dense_smoke_cli_emits_only_normalized_vector_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeEncoder:
        revision = "5617a9f61b028005a4858fdac845db406aefb181"

        @classmethod
        def from_lock(cls, *args: object, **kwargs: object) -> FakeEncoder:
            return cls()

        def encode(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            assert len(texts) == 1
            return ((0.6, 0.8),)

    monkeypatch.setattr(cli_module, "DenseEncoder", FakeEncoder)
    monkeypatch.setenv("SEN_QA_EMBEDDING_MODEL_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("SEN_QA_EMBEDDING_LOCK_SHA256", "f" * 64)
    sentinel = "PRIVATE_SMOKE_TEXT"

    result = CliRunner().invoke(app, ["dense-smoke", "--text", sentinel])

    assert result.exit_code == 0
    assert result.stdout.strip() == (
        "vectors=1 dimension=2 normalized=1 revision=5617a9f6 failed=0"
    )
    assert sentinel not in result.stdout


def test_embedding_verification_cli_emits_only_lock_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "models.lock.json"
    lock_path.write_text("{}", encoding="utf-8")
    model_root = tmp_path / "cache"
    model_root.mkdir()
    lock = SimpleNamespace(
        files=(object(), object()),
        revision="5617a9f61b028005a4858fdac845db406aefb181",
    )
    verified: list[tuple[object, Path, str, str]] = []
    monkeypatch.setattr(cli_module, "load_embedding_model_lock", lambda _: lock)
    monkeypatch.setattr(
        cli_module,
        "verify_embedding_cache",
        lambda value, root, *, scope, expected_lock_sha256: verified.append(
            (value, root, scope, expected_lock_sha256)
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "verify-embedding-models",
            "--lock",
            str(lock_path),
            "--model-root",
            str(model_root),
            "--expected-lock-sha256",
            "f" * 64,
        ],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "models=1 files=2 revision=5617a9f6 failed=0"
    assert verified == [(lock, model_root, "full", "f" * 64)]


def test_embedding_verification_cli_sanitizes_lock_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "models.lock.json"
    lock_path.write_text("{}", encoding="utf-8")
    model_root = tmp_path / "cache"
    model_root.mkdir()

    def fail(_: Path) -> object:
        raise ChunkingError("PRIVATE_LOCK_SENTINEL")

    monkeypatch.setattr(cli_module, "load_embedding_model_lock", fail)
    result = CliRunner().invoke(
        app,
        [
            "verify-embedding-models",
            "--lock",
            str(lock_path),
            "--model-root",
            str(model_root),
            "--expected-lock-sha256",
            "f" * 64,
        ],
    )

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=embedding_cache_invalid"
    assert "PRIVATE" not in result.stdout
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None


def test_dense_smoke_cli_sanitizes_failure_and_exception_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FailedEncoder:
        @classmethod
        def from_lock(cls, *args: object, **kwargs: object) -> object:
            raise DenseError("embedding_cache_invalid")

    monkeypatch.setattr(cli_module, "DenseEncoder", FailedEncoder)
    monkeypatch.setenv("SEN_QA_EMBEDDING_MODEL_ROOT", str(tmp_path / "cache"))
    monkeypatch.setenv("SEN_QA_EMBEDDING_LOCK_SHA256", "f" * 64)

    result = CliRunner().invoke(app, ["dense-smoke", "--text", "PRIVATE_SMOKE_TEXT"])

    assert result.exit_code == 1
    assert result.stdout.strip() == "failed=1 error_code=embedding_cache_invalid"
    assert "PRIVATE" not in result.stdout
    assert result.exception is not None
    assert result.exception.__cause__ is None
    assert result.exception.__context__ is None


def test_indexer_image_is_pinned_offline_nonroot_and_uses_index_extra() -> None:
    dockerfile = Path("docker/indexer.Dockerfile").read_text(encoding="utf-8")
    preparer = Path("docker/prepare_embedding_model.py").read_text(encoding="utf-8")

    assert dockerfile.count("FROM --platform=linux/amd64 ") >= 2
    assert all(
        "@sha256:" in line
        for line in dockerfile.splitlines()
        if line.startswith("FROM ")
    )
    assert "uv sync --frozen --extra index --no-dev" in dockerfile
    assert "prepare_embedding_model.py" in dockerfile
    assert "verify-embedding-models" in dockerfile
    assert "install -d -m 0755 /opt/models" in dockerfile
    assert "HF_HUB_OFFLINE=1" in dockerfile
    assert "TRANSFORMERS_OFFLINE=1" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert 'ENTRYPOINT ["/opt/venv/bin/python", "-m", "src.cli"]' in dockerfile
    assert "find /opt/models/bge-m3 -type d -exec chmod 0555 {} +" in dockerfile
    assert "find /opt/models/bge-m3 -type f -exec chmod 0444 {} +" in dockerfile
    assert "5617a9f61b028005a4858fdac845db406aefb181" in dockerfile
    assert " main" not in dockerfile
    assert " latest" not in dockerfile
    assert "response.geturl() != source_url" not in preparer
    assert "hmac.compare_digest(digest.hexdigest(), sha256)" in preparer


def test_qdrant_client_rejects_nonlocal_endpoints_before_network_io() -> None:
    with pytest.raises(DenseError, match="qdrant_endpoint_invalid") as captured:
        create_qdrant_client("https://unreviewed.example.invalid")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dense_snapshot_is_created_and_downloaded_from_exact_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"synthetic-qdrant-snapshot"
    checksum = hashlib.sha256(payload).hexdigest()
    calls: list[tuple[object, ...]] = []

    class Client:
        def create_snapshot(self, collection_name: str, *, wait: bool) -> object:
            calls.append(("create", collection_name, wait))
            return SimpleNamespace(
                name="candidate.snapshot",
                checksum=checksum,
                size=len(payload),
            )

    class Response:
        status = 200

        def read(self, amount: int) -> bytes:
            chunk, self.remaining = self.remaining[:amount], self.remaining[amount:]
            return chunk

        remaining = payload

    class Connection:
        def request(self, method: str, path: str) -> None:
            calls.append(("request", method, path))

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            calls.append(("close",))

    monkeypatch.setattr(
        dense_module,
        "_snapshot_http_connection_provider",
        lambda host, port, timeout: (
            calls.append(("connect", host, port, timeout)) or Connection()
        ),
    )
    output = tmp_path / "qdrant.snapshot"
    result = export_dense_snapshot(
        Client(),
        qdrant_url="http://qdrant:6333",
        collection_name=f"{RELEASE_ID}-bge-m3",
        output=output,
    )

    assert output.read_bytes() == payload
    assert result.sha256 == checksum
    assert result.size == len(payload)
    assert result.collection_name == f"{RELEASE_ID}-bge-m3"
    assert calls[:3] == [
        ("create", f"{RELEASE_ID}-bge-m3", True),
        ("connect", "qdrant", 6333, 60),
        (
            "request",
            "GET",
            f"/collections/{RELEASE_ID}-bge-m3/snapshots/candidate.snapshot",
        ),
    ]
    assert calls[-1] == ("close",)


def test_dense_snapshot_hash_mismatch_removes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        def create_snapshot(self, collection_name: str, *, wait: bool) -> object:
            return SimpleNamespace(
                name="candidate.snapshot",
                checksum="f" * 64,
                size=3,
            )

    class Response:
        status = 200
        returned = False

        def read(self, amount: int) -> bytes:
            if self.returned:
                return b""
            self.returned = True
            return b"bad"

    class Connection:
        def request(self, method: str, path: str) -> None:
            pass

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        dense_module,
        "_snapshot_http_connection_provider",
        lambda host, port, timeout: Connection(),
    )
    output = tmp_path / "qdrant.snapshot"

    with pytest.raises(DenseError, match="dense_snapshot_invalid") as captured:
        export_dense_snapshot(
            Client(),
            qdrant_url="http://qdrant:6333",
            collection_name=f"{RELEASE_ID}-bge-m3",
            output=output,
        )

    assert not output.exists()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_dense_snapshot_rejects_unapproved_endpoint_before_client_io(
    tmp_path: Path,
) -> None:
    called = False

    class Client:
        def create_snapshot(self, collection_name: str, *, wait: bool) -> object:
            nonlocal called
            called = True
            raise AssertionError("network must not be reached")

    with pytest.raises(DenseError, match="dense_snapshot_invalid"):
        export_dense_snapshot(
            Client(),
            qdrant_url="http://unapproved.invalid:6333",
            collection_name=f"{RELEASE_ID}-bge-m3",
            output=tmp_path / "qdrant.snapshot",
        )

    assert called is False


def test_qdrant_compose_is_digest_pinned_local_only_and_memory_bounded() -> None:
    compose = Path("docker-compose.index.yml").read_text(encoding="utf-8")

    assert (
        "qdrant/qdrant:v1.18.3-unprivileged@sha256:"
        "affb67e1d6f2f93d7d20b90d238a7d4b974d36351c162e73bda794e4b2e03483"
    ) in compose
    assert '"127.0.0.1:6333:6333"' in compose
    assert compose.count("pull_policy: never") == 2
    assert "mem_limit: 2g" in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert re.search(r"cap_drop:\s*\n\s*- ALL", compose)
    assert "latest" not in compose


def test_docker_context_allows_only_reviewed_indexer_inputs() -> None:
    rules = Path(".dockerignore").read_text(encoding="utf-8")

    assert "!docker/indexer.Dockerfile" in rules
    assert "!docker/prepare_embedding_model.py" in rules
    assert "!docker/arbitrary.py" not in rules
