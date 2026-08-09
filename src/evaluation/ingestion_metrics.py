"""Deterministic ingestion-quality aggregates for release gating."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.corpus.ids import validate_case_id

_MAX_OBSERVATIONS = 200
_MAX_CASES_PER_OBSERVATION = 256


class IngestionMetricError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise IngestionMetricError(code) from None


class IngestionObservation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        hide_input_in_errors=True,
    )

    item_id: str = Field(pattern=r"^eval-(?:dev|blind)-[0-9]{3,4}$")
    gold_case_ids: tuple[str, ...] = Field(max_length=_MAX_CASES_PER_OBSERVATION)
    predicted_case_ids: tuple[str, ...] = Field(max_length=_MAX_CASES_PER_OBSERVATION)
    blind: bool
    page_anchors_checked: int = Field(ge=0, le=1_000_000)
    page_anchors_correct: int = Field(ge=0, le=1_000_000)
    bleed_count: int = Field(ge=0, le=1_000_000)
    split_count: int = Field(ge=0, le=1_000_000)
    missing_required_fields: int = Field(ge=0, le=1_000_000)
    critical_entity_errors: int = Field(ge=0, le=1_000_000)
    truncated_1502_count: int = Field(ge=0, le=1_000_000)
    provenance_missing_count: int = Field(ge=0, le=1_000_000)

    @model_validator(mode="after")
    def has_consistent_counts_and_unique_cases(self) -> IngestionObservation:
        case_ids = (*self.gold_case_ids, *self.predicted_case_ids)
        if (
            self.page_anchors_correct > self.page_anchors_checked
            or len(set(self.gold_case_ids)) != len(self.gold_case_ids)
            or len(set(self.predicted_case_ids)) != len(self.predicted_case_ids)
            or any(not _valid_case_id(case_id) for case_id in case_ids)
        ):
            raise ValueError("ingestion observation is invalid")
        return self


def _valid_case_id(value: object) -> bool:
    try:
        return type(value) is str and validate_case_id(value) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class IngestionMetrics:
    observations: int
    true_positive_boundaries: int
    false_positive_boundaries: int
    false_negative_boundaries: int
    boundary_precision: float
    boundary_recall: float
    boundary_f1: float
    bleed_count: int
    split_count: int
    missing_required_fields: int
    page_anchors_checked: int
    page_anchors_correct: int
    page_anchor_accuracy: float
    blind_page_anchors_checked: int
    blind_page_anchors_correct: int
    blind_page_anchor_accuracy: float
    critical_entity_errors: int
    truncated_1502_count: int
    provenance_missing_count: int


def _revalidate(value: object) -> IngestionObservation | None:
    if type(value) is not IngestionObservation:
        return None
    try:
        fields = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(fields) is not dict or set(fields) != set(
        IngestionObservation.model_fields
    ):
        return None
    try:
        return IngestionObservation.model_validate(dict(fields))
    except (ValidationError, TypeError, ValueError):
        return None


def _ratio(numerator: int, denominator: int) -> float:
    return 1.0 if denominator == 0 else numerator / denominator


def evaluate_ingestion(observations: object) -> IngestionMetrics:
    if (
        type(observations) is not tuple
        or not observations
        or len(observations) > _MAX_OBSERVATIONS
    ):
        _raise("ingestion_metrics_invalid")
    checked = tuple(
        _revalidate(item) for item in cast(tuple[object, ...], observations)
    )
    if any(item is None for item in checked):
        _raise("ingestion_metrics_invalid")
    items = cast(tuple[IngestionObservation, ...], checked)
    if len({item.item_id for item in items}) != len(items):
        _raise("ingestion_metrics_invalid")
    true_positive = 0
    false_positive = 0
    false_negative = 0
    for item in items:
        gold = set(item.gold_case_ids)
        predicted = set(item.predicted_case_ids)
        true_positive += len(gold & predicted)
        false_positive += len(predicted - gold)
        false_negative += len(gold - predicted)
    precision = _ratio(true_positive, true_positive + false_positive)
    recall = _ratio(true_positive, true_positive + false_negative)
    f1 = (
        0.0
        if precision + recall == 0
        else 2.0 * precision * recall / (precision + recall)
    )
    anchors_checked = sum(item.page_anchors_checked for item in items)
    anchors_correct = sum(item.page_anchors_correct for item in items)
    blind_checked = sum(item.page_anchors_checked for item in items if item.blind)
    blind_correct = sum(item.page_anchors_correct for item in items if item.blind)
    return IngestionMetrics(
        observations=len(items),
        true_positive_boundaries=true_positive,
        false_positive_boundaries=false_positive,
        false_negative_boundaries=false_negative,
        boundary_precision=precision,
        boundary_recall=recall,
        boundary_f1=f1,
        bleed_count=sum(item.bleed_count for item in items),
        split_count=sum(item.split_count for item in items),
        missing_required_fields=sum(item.missing_required_fields for item in items),
        page_anchors_checked=anchors_checked,
        page_anchors_correct=anchors_correct,
        page_anchor_accuracy=_ratio(anchors_correct, anchors_checked),
        blind_page_anchors_checked=blind_checked,
        blind_page_anchors_correct=blind_correct,
        blind_page_anchor_accuracy=_ratio(blind_correct, blind_checked),
        critical_entity_errors=sum(item.critical_entity_errors for item in items),
        truncated_1502_count=sum(item.truncated_1502_count for item in items),
        provenance_missing_count=sum(item.provenance_missing_count for item in items),
    )


def ingestion_release_ready(value: object) -> bool:
    if type(value) is not IngestionMetrics:
        return False
    report = value
    return (
        report.boundary_f1 == 1.0
        and report.bleed_count == 0
        and report.split_count == 0
        and report.missing_required_fields == 0
        and report.page_anchors_checked > 0
        and report.page_anchor_accuracy == 1.0
        and report.blind_page_anchors_checked > 0
        and report.blind_page_anchor_accuracy == 1.0
        and report.critical_entity_errors == 0
        and report.truncated_1502_count == 0
        and report.provenance_missing_count == 0
    )
