from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from pydantic import BaseModel

from src.evaluation.goldset import ReleaseGoldItem, combine_release_goldsets
from src.evaluation.ingestion_metrics import IngestionObservation
from src.evaluation.release_report import (
    ReleaseReportError,
    build_release_evaluation_report,
    canonical_release_evaluation_bytes,
    create_release_evaluation_report,
    load_canonical_evidence,
    load_ingestion_observations,
    load_retrieval_observations,
)
from src.evaluation.retrieval_metrics import RetrievalObservation, StageLatency
from tests.evaluation.test_goldset import (
    _blind_label,
    _blind_question,
    _case_id,
    _dev_item,
)

RELEASE_ID = "corpus-20250808123456-deadbeef"


def _gold() -> tuple[ReleaseGoldItem, ...]:
    return combine_release_goldsets(
        tuple(_dev_item(index) for index in range(140)),
        tuple(_blind_question(index) for index in range(60)),
        tuple(_blind_label(index) for index in range(60)),
        canonical_evidence={
            _case_id(year): frozenset(((13, 0),)) for year in range(2020, 2026)
        },
    )


def _ingestion(
    gold: tuple[ReleaseGoldItem, ...],
) -> tuple[IngestionObservation, ...]:
    return tuple(
        IngestionObservation(
            item_id=item.item_id,
            gold_case_ids=item.accepted_case_ids,
            predicted_case_ids=item.accepted_case_ids,
            blind=item.item_id.startswith("eval-blind-"),
            page_anchors_checked=max(1, len(item.required_evidence)),
            page_anchors_correct=max(1, len(item.required_evidence)),
            bleed_count=0,
            split_count=0,
            missing_required_fields=0,
            critical_entity_errors=0,
            truncated_1502_count=0,
            provenance_missing_count=0,
        )
        for item in gold
    )


def _retrieval(
    gold: tuple[ReleaseGoldItem, ...],
) -> tuple[RetrievalObservation, ...]:
    return tuple(
        RetrievalObservation(
            item_id=item.item_id,
            edition_year=item.edition_year,
            ocr_quality_group=(
                "none"
                if item.edition_year <= 2022
                else "low_resolution"
                if item.low_resolution_ocr
                else "high_resolution"
            ),
            no_answer_expected=item.no_answer,
            no_answer_candidate=item.no_answer,
            accepted_case_ids=item.accepted_case_ids,
            ranked_case_ids=item.accepted_case_ids,
            evidence_case_ids=item.accepted_case_ids,
            latency=StageLatency(
                normalization_ms=1.0,
                lexical_ms=2.0,
                dense_ms=3.0,
                fusion_ms=1.0,
                parent_expansion_ms=1.0,
                total_ms=8.0,
            ),
        )
        for item in gold
    )


def test_release_report_binds_all_200_gold_items_and_emits_aggregates_only() -> None:
    gold = _gold()
    observations = _retrieval(gold)

    report = build_release_evaluation_report(
        release_id=RELEASE_ID,
        canonical_database_sha256="a" * 64,
        retrieval_index_sha256="b" * 64,
        gold_items=gold,
        ingestion_observations=_ingestion(gold),
        retrieval_observations={
            system: observations
            for system in ("substring", "lexical", "dense", "hybrid")
        },
    )
    rendered = canonical_release_evaluation_bytes(report)
    payload = json.loads(rendered)

    assert report.gold_items == 200
    assert report.blind_items == 60
    assert report.ingestion_gate is True
    assert report.retrieval_gate is True
    assert payload["schema_version"] == "sen-qa-release-evaluation/v1"
    assert payload["canonical_database_sha256"] == "a" * 64
    assert payload["retrieval_index_sha256"] == "b" * 64
    assert '"item_id":' not in rendered.decode("ascii")
    assert '"question":' not in rendered.decode("ascii")


def test_release_report_rejects_observations_with_self_asserted_gold_labels() -> None:
    gold = _gold()
    retrieval = list(_retrieval(gold))
    first = retrieval[15]
    retrieval[15] = first.model_copy(update={"accepted_case_ids": ()})

    with pytest.raises(ReleaseReportError, match="evaluation_observations_invalid"):
        build_release_evaluation_report(
            release_id=RELEASE_ID,
            canonical_database_sha256="a" * 64,
            retrieval_index_sha256="b" * 64,
            gold_items=gold,
            ingestion_observations=_ingestion(gold),
            retrieval_observations={
                system: tuple(retrieval)
                for system in ("substring", "lexical", "dense", "hybrid")
            },
        )


def _write_jsonl(path: Path, records: tuple[BaseModel, ...]) -> None:
    path.write_text(
        "".join(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_observation_loaders_require_exact_private_regular_jsonl(
    tmp_path: Path,
) -> None:
    gold = _gold()
    ingestion_path = tmp_path / "ingestion.jsonl"
    _write_jsonl(ingestion_path, _ingestion(gold))
    retrieval_paths: dict[str, Path] = {}
    for system in ("substring", "lexical", "dense", "hybrid"):
        path = tmp_path / f"{system}.jsonl"
        _write_jsonl(path, _retrieval(gold))
        retrieval_paths[system] = path

    assert len(load_ingestion_observations(ingestion_path)) == 200
    assert all(
        len(records) == 200
        for records in load_retrieval_observations(retrieval_paths).values()
    )

    linked = tmp_path / "linked.jsonl"
    linked.symlink_to(ingestion_path)
    with pytest.raises(ReleaseReportError, match="evaluation_observations_invalid"):
        load_ingestion_observations(linked)

    ingestion_path.chmod(0o640)
    with pytest.raises(ReleaseReportError, match="evaluation_observations_invalid"):
        load_ingestion_observations(ingestion_path)


def test_canonical_evidence_comes_only_from_searchable_case_span_rows(
    tmp_path: Path,
) -> None:
    database = tmp_path / "canonical.sqlite3"
    case_id = _case_id(2025)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE cases("
            "case_id TEXT PRIMARY KEY, review_status TEXT NOT NULL, "
            "search_eligible INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE source_spans("
            "case_id TEXT NOT NULL, span_index INTEGER NOT NULL, "
            "pdf_page_index INTEGER NOT NULL, PRIMARY KEY(case_id,span_index))"
        )
        connection.execute(
            "INSERT INTO cases VALUES(?,?,?)",
            (case_id, "approved", 1),
        )
        connection.execute(
            "INSERT INTO source_spans VALUES(?,?,?)",
            (case_id, 0, 13),
        )
        connection.execute(
            "INSERT INTO cases VALUES(?,?,?)",
            (_case_id(2024), "rejected", 0),
        )
        connection.execute(
            "INSERT INTO source_spans VALUES(?,?,?)",
            (_case_id(2024), 0, 14),
        )

    evidence = load_canonical_evidence(database)

    assert evidence == {case_id: frozenset(((13, 0),))}
    assert os.stat(database).st_nlink == 1


def test_file_evaluation_writes_one_private_aggregate_report_without_clobber(
    tmp_path: Path,
) -> None:
    gold = _gold()
    dev_path = tmp_path / "retrieval-dev.jsonl"
    blind_path = tmp_path / "retrieval-blind.jsonl"
    labels_path = tmp_path / "retrieval-blind-labels.jsonl"
    _write_jsonl(dev_path, tuple(_dev_item(index) for index in range(140)))
    _write_jsonl(blind_path, tuple(_blind_question(index) for index in range(60)))
    _write_jsonl(labels_path, tuple(_blind_label(index) for index in range(60)))
    ingestion_path = tmp_path / "ingestion.jsonl"
    _write_jsonl(ingestion_path, _ingestion(gold))
    retrieval_paths: dict[str, Path] = {}
    for system in ("substring", "lexical", "dense", "hybrid"):
        path = tmp_path / f"{system}.jsonl"
        _write_jsonl(path, _retrieval(gold))
        retrieval_paths[system] = path
    retrieval_index = tmp_path / "qdrant.snapshot"
    retrieval_index.write_bytes(b"qdrant-snapshot")
    database = tmp_path / "canonical.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE cases("
            "case_id TEXT PRIMARY KEY, review_status TEXT NOT NULL, "
            "search_eligible INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE source_spans("
            "case_id TEXT NOT NULL, span_index INTEGER NOT NULL, "
            "pdf_page_index INTEGER NOT NULL, PRIMARY KEY(case_id,span_index))"
        )
        for year in range(2020, 2026):
            connection.execute(
                "INSERT INTO cases VALUES(?,?,?)",
                (_case_id(year), "approved", 1),
            )
            connection.execute(
                "INSERT INTO source_spans VALUES(?,?,?)",
                (_case_id(year), 0, 13),
            )
    report_dir = tmp_path / "reports"
    report_dir.mkdir(mode=0o700)
    output = report_dir / "evaluation-report.json"

    report = create_release_evaluation_report(
        release_id=RELEASE_ID,
        canonical_database=database,
        retrieval_index=retrieval_index,
        dev_gold=dev_path,
        blind_gold=blind_path,
        blind_labels=labels_path,
        ingestion_path=ingestion_path,
        retrieval_paths=retrieval_paths,
        output=output,
    )

    assert report.ingestion_gate and report.retrieval_gate
    assert os.stat(output).st_mode & 0o777 == 0o600
    assert output.read_bytes() == canonical_release_evaluation_bytes(report)
    with pytest.raises(ReleaseReportError, match="evaluation_report_write_failed"):
        create_release_evaluation_report(
            release_id=RELEASE_ID,
            canonical_database=database,
            retrieval_index=retrieval_index,
            dev_gold=dev_path,
            blind_gold=blind_path,
            blind_labels=labels_path,
            ingestion_path=ingestion_path,
            retrieval_paths=retrieval_paths,
            output=output,
        )
