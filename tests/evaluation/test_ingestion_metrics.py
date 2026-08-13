from __future__ import annotations

import pytest

from src.corpus.ids import make_case_id
from src.evaluation.ingestion_metrics import (
    IngestionMetricError,
    IngestionObservation,
    evaluate_ingestion,
    ingestion_release_ready,
)

CASE_A = make_case_id(2020, "계약", "계약", "a")
CASE_B = make_case_id(2021, "계약", "계약", "b")
CASE_EXTRA = make_case_id(2022, "계약", "계약", "extra")


def _observation(
    item_id: str,
    *,
    gold: tuple[str, ...],
    predicted: tuple[str, ...],
    blind: bool = False,
    anchors_checked: int = 1,
    anchors_correct: int = 1,
    bleed: int = 0,
    split: int = 0,
    missing: int = 0,
    entity_errors: int = 0,
    truncated: int = 0,
    provenance_missing: int = 0,
) -> IngestionObservation:
    return IngestionObservation(
        item_id=item_id,
        gold_case_ids=gold,
        predicted_case_ids=predicted,
        blind=blind,
        page_anchors_checked=anchors_checked,
        page_anchors_correct=anchors_correct,
        bleed_count=bleed,
        split_count=split,
        missing_required_fields=missing,
        critical_entity_errors=entity_errors,
        truncated_1502_count=truncated,
        provenance_missing_count=provenance_missing,
    )


def test_perfect_ingestion_metrics_pass_every_release_gate() -> None:
    report = evaluate_ingestion(
        (
            _observation("eval-dev-001", gold=(CASE_A,), predicted=(CASE_A,)),
            _observation(
                "eval-blind-001",
                gold=(CASE_B,),
                predicted=(CASE_B,),
                blind=True,
            ),
        )
    )

    assert report.boundary_precision == 1.0
    assert report.boundary_recall == 1.0
    assert report.boundary_f1 == 1.0
    assert report.page_anchor_accuracy == 1.0
    assert report.blind_page_anchor_accuracy == 1.0
    assert ingestion_release_ready(report) is True


def test_ingestion_metrics_count_false_boundaries_and_every_blocker() -> None:
    report = evaluate_ingestion(
        (
            _observation(
                "eval-dev-001",
                gold=(CASE_A, CASE_B),
                predicted=(CASE_A, CASE_EXTRA),
                anchors_checked=2,
                anchors_correct=1,
                bleed=1,
                split=2,
                missing=3,
                entity_errors=4,
                truncated=5,
                provenance_missing=6,
            ),
        )
    )

    assert report.true_positive_boundaries == 1
    assert report.false_positive_boundaries == 1
    assert report.false_negative_boundaries == 1
    assert report.boundary_precision == 0.5
    assert report.boundary_recall == 0.5
    assert report.boundary_f1 == 0.5
    assert report.page_anchor_accuracy == 0.5
    assert (report.bleed_count, report.split_count) == (1, 2)
    assert report.missing_required_fields == 3
    assert report.critical_entity_errors == 4
    assert report.truncated_1502_count == 5
    assert report.provenance_missing_count == 6
    assert ingestion_release_ready(report) is False


def test_ingestion_metrics_reject_duplicate_or_unbounded_observations() -> None:
    duplicate = _observation("eval-dev-001", gold=(CASE_A,), predicted=(CASE_A,))

    with pytest.raises(IngestionMetricError, match="ingestion_metrics_invalid"):
        evaluate_ingestion((duplicate, duplicate))

    with pytest.raises(IngestionMetricError, match="ingestion_metrics_invalid"):
        evaluate_ingestion(tuple(duplicate for _ in range(201)))


def test_ingestion_metrics_reject_noncanonical_case_ids_without_echoing() -> None:
    forged = IngestionObservation.model_construct(
        **{
            **duplicate_observation().__dict__,
            "predicted_case_ids": ("PRIVATE_CASE_SENTINEL",),
        }
    )

    with pytest.raises(
        IngestionMetricError, match="ingestion_metrics_invalid"
    ) as captured:
        evaluate_ingestion((forged,))

    assert "PRIVATE" not in repr(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def duplicate_observation() -> IngestionObservation:
    return _observation("eval-dev-999", gold=(CASE_A,), predicted=(CASE_A,))
