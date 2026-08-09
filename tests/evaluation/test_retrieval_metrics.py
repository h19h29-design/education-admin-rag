from __future__ import annotations

import math

import pytest

from src.corpus.ids import make_case_id
from src.evaluation.retrieval_metrics import (
    RetrievalMetricError,
    RetrievalObservation,
    StageLatency,
    evaluate_retrieval,
    retrieval_release_ready,
)

CASE_CORRECT = make_case_id(2020, "계약", "계약", "correct")
CASE_WRONG = make_case_id(2020, "계약", "계약", "wrong")


def _latency(
    *, total_ms: float = 100.0, cold_start_ms: float | None = None
) -> StageLatency:
    return StageLatency(
        normalization_ms=5.0,
        lexical_ms=20.0,
        dense_ms=40.0,
        fusion_ms=10.0,
        parent_expansion_ms=25.0,
        total_ms=total_ms,
        cold_start_ms=cold_start_ms,
    )


def _positive(
    item_id: str,
    year: int,
    *,
    ranked: tuple[str, ...] = (CASE_CORRECT,),
    evidence: tuple[str, ...] = (CASE_CORRECT,),
    ocr_quality_group: str = "none",
) -> RetrievalObservation:
    return RetrievalObservation(
        item_id=item_id,
        edition_year=year,
        ocr_quality_group=ocr_quality_group,  # type: ignore[arg-type]
        no_answer_expected=False,
        no_answer_candidate=False,
        accepted_case_ids=(CASE_CORRECT,),
        ranked_case_ids=ranked,
        evidence_case_ids=evidence,
        latency=_latency(),
    )


def _no_answer(
    item_id: str, year: int, *, predicted: bool = True
) -> RetrievalObservation:
    return RetrievalObservation(
        item_id=item_id,
        edition_year=year,
        ocr_quality_group="none",
        no_answer_expected=True,
        no_answer_candidate=predicted,
        accepted_case_ids=(),
        ranked_case_ids=(),
        evidence_case_ids=(),
        latency=_latency(cold_start_ms=500.0),
    )


def test_perfect_hybrid_metrics_pass_global_year_ocr_and_latency_gates() -> None:
    observations: list[RetrievalObservation] = []
    for year in range(2020, 2026):
        observations.append(
            _positive(
                f"eval-dev-{year}",
                year,
                ocr_quality_group="low_resolution" if year in {2023, 2024} else "none",
            )
        )
        observations.append(_no_answer(f"eval-blind-{year}", year))

    report = evaluate_retrieval("hybrid", tuple(observations))

    assert report.recall_at_10 == 1.0
    assert report.mrr_at_10 == 1.0
    assert report.ndcg_at_10 == 1.0
    assert report.evidence_span_coverage == 1.0
    assert report.no_answer_recall == 1.0
    assert dict(report.recall_at_10_by_year) == {
        year: 1.0 for year in range(2020, 2026)
    }
    assert report.warm_latency_p95_ms.total_ms == 100.0
    assert report.cold_start_p95_ms == 500.0
    assert retrieval_release_ready(report) is True


def test_retrieval_metrics_use_ranked_relevance_and_keep_blind_results_aggregate() -> (
    None
):
    report = evaluate_retrieval(
        "lexical",
        (
            _positive(
                "eval-dev-001",
                2020,
                ranked=(CASE_WRONG, CASE_CORRECT),
            ),
            _positive(
                "eval-dev-002",
                2021,
                ranked=(CASE_WRONG,),
                evidence=(),
            ),
            _no_answer("eval-blind-001", 2022),
            _no_answer("eval-blind-002", 2023, predicted=False),
        ),
    )

    assert report.recall_at_10 == 0.5
    assert report.mrr_at_10 == 0.25
    assert report.ndcg_at_10 == pytest.approx(1.0 / math.log2(3) / 2)
    assert report.evidence_span_coverage == 1.0
    assert report.no_answer_recall == 0.5
    assert not hasattr(report, "item_results")
    assert retrieval_release_ready(report) is False


def test_retrieval_metrics_reject_duplicate_ids_and_invalid_evidence() -> None:
    valid = _positive("eval-dev-001", 2020)
    invalid = RetrievalObservation.model_construct(
        **{
            **valid.__dict__,
            "evidence_case_ids": (CASE_WRONG,),
        }
    )

    with pytest.raises(RetrievalMetricError, match="retrieval_metrics_invalid"):
        evaluate_retrieval("hybrid", (invalid,))

    with pytest.raises(RetrievalMetricError, match="retrieval_metrics_invalid"):
        evaluate_retrieval("hybrid", (valid, valid))


def test_retrieval_metrics_reject_noncanonical_case_ids_without_echoing() -> None:
    valid = _positive("eval-dev-001", 2020)
    forged = RetrievalObservation.model_construct(
        **{
            **valid.__dict__,
            "ranked_case_ids": ("PRIVATE_CASE_SENTINEL",),
            "evidence_case_ids": (),
        }
    )

    with pytest.raises(
        RetrievalMetricError, match="retrieval_metrics_invalid"
    ) as captured:
        evaluate_retrieval("hybrid", (forged,))

    assert "PRIVATE" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
