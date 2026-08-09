"""Canonical semantic identity contracts."""

from __future__ import annotations

import pytest

from src.corpus.build import BuildError, canonical_content_sha256

_CONTENT_FILES = (
    "case_authorities.jsonl",
    "case_relations.jsonl",
    "cases.jsonl",
    "chunk_source_spans.jsonl",
    "chunks.jsonl",
    "corrections.jsonl",
    "documents.jsonl",
    "issued_case_ids.jsonl",
    "law_refs.jsonl",
    "review_events.jsonl",
    "review_registry.jsonl",
    "review_registry_locations.jsonl",
    "source_spans.jsonl",
    "tokenizer_contract.jsonl",
)


def test_semantic_hash_excludes_release_and_sqlite_physical_artifacts() -> None:
    first = {name: f"{index:064x}" for index, name in enumerate(_CONTENT_FILES, 1)}
    first.update(
        {
            "build_meta.jsonl": "a" * 64,
            "ingestion_runs.jsonl": "b" * 64,
        }
    )
    second = {
        **first,
        "build_meta.jsonl": "d" * 64,
        "ingestion_runs.jsonl": "e" * 64,
    }

    assert canonical_content_sha256(first) == canonical_content_sha256(second)
    changed_tombstone = {**second, "issued_case_ids.jsonl": "f" * 64}
    assert canonical_content_sha256(first) != canonical_content_sha256(
        changed_tombstone
    )


def test_semantic_hash_requires_every_canonical_content_table() -> None:
    incomplete = {name: "a" * 64 for name in _CONTENT_FILES[:-1]}
    with pytest.raises(BuildError, match="incomplete"):
        canonical_content_sha256(incomplete)
