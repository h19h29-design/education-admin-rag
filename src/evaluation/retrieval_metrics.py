"""Aggregate-only retrieval, evidence, no-answer, and latency evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.corpus.ids import validate_case_id

SearchSystem = Literal["substring", "lexical", "dense", "hybrid"]
OcrQualityGroup = Literal["none", "low_resolution", "high_resolution"]

_MAX_OBSERVATIONS = 200
_MAX_RANKED_RESULTS = 100
_MAX_IDENTIFIER_CHARACTERS = 200


class RetrievalMetricError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _raise(code: str) -> NoReturn:
    raise RetrievalMetricError(code) from None


class MetricModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class StageLatency(MetricModel):
    normalization_ms: float = Field(ge=0, le=86_400_000)
    lexical_ms: float = Field(ge=0, le=86_400_000)
    dense_ms: float = Field(ge=0, le=86_400_000)
    fusion_ms: float = Field(ge=0, le=86_400_000)
    parent_expansion_ms: float = Field(ge=0, le=86_400_000)
    total_ms: float = Field(ge=0, le=86_400_000)
    cold_start_ms: float | None = Field(default=None, ge=0, le=86_400_000)

    @model_validator(mode="after")
    def total_covers_each_warm_stage(self) -> StageLatency:
        if self.total_ms < max(
            self.normalization_ms,
            self.lexical_ms,
            self.dense_ms,
            self.fusion_ms,
            self.parent_expansion_ms,
        ):
            raise ValueError("latency total is inconsistent")
        return self


class RetrievalObservation(MetricModel):
    item_id: str = Field(pattern=r"^eval-(?:dev|blind)-[0-9]{3,4}$")
    edition_year: int = Field(ge=2020, le=2025)
    ocr_quality_group: OcrQualityGroup
    no_answer_expected: bool
    no_answer_candidate: bool
    accepted_case_ids: tuple[str, ...] = Field(max_length=16)
    ranked_case_ids: tuple[str, ...] = Field(max_length=_MAX_RANKED_RESULTS)
    evidence_case_ids: tuple[str, ...] = Field(max_length=_MAX_RANKED_RESULTS)
    latency: StageLatency

    @model_validator(mode="after")
    def has_consistent_relevance_and_evidence(self) -> RetrievalObservation:
        identifier_groups = (
            self.accepted_case_ids,
            self.ranked_case_ids,
            self.evidence_case_ids,
        )
        if (
            any(len(set(group)) != len(group) for group in identifier_groups)
            or any(
                not _valid_case_id(value)
                for group in identifier_groups
                for value in group
            )
            or not set(self.evidence_case_ids).issubset(self.ranked_case_ids)
            or (self.no_answer_expected and bool(self.accepted_case_ids))
            or (not self.no_answer_expected and not self.accepted_case_ids)
        ):
            raise ValueError("retrieval observation is invalid")
        return self


def _valid_case_id(value: object) -> bool:
    if type(value) is not str or len(value) > _MAX_IDENTIFIER_CHARACTERS:
        return False
    try:
        return validate_case_id(value) == value
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class LatencyP95:
    normalization_ms: float
    lexical_ms: float
    dense_ms: float
    fusion_ms: float
    parent_expansion_ms: float
    total_ms: float


@dataclass(frozen=True, slots=True)
class RetrievalMetrics:
    system: SearchSystem
    observations: int
    positive_questions: int
    no_answer_questions: int
    recall_at_10: float
    recall_at_10_by_year: tuple[tuple[int, float], ...]
    mrr_at_10: float
    ndcg_at_10: float
    evidence_span_coverage: float
    no_answer_recall: float
    ocr_recall_at_10: tuple[tuple[str, float], ...]
    warm_latency_p95_ms: LatencyP95
    cold_start_p95_ms: float | None


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


def _revalidate(value: object) -> RetrievalObservation | None:
    fields = _model_fields(value, RetrievalObservation)
    if fields is None:
        return None
    latency = _model_fields(fields.get("latency"), StageLatency)
    if latency is None:
        return None
    fields["latency"] = latency
    try:
        return RetrievalObservation.model_validate(fields)
    except (ValidationError, TypeError, ValueError):
        return None


def _p95(values: tuple[float, ...]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _positive_metrics(
    items: tuple[RetrievalObservation, ...],
) -> tuple[float, float, float, float]:
    if not items:
        return 0.0, 0.0, 0.0, 0.0
    recalled = 0
    reciprocal_ranks = 0.0
    ndcg = 0.0
    hits_with_evidence = 0
    for item in items:
        accepted = set(item.accepted_case_ids)
        ranked = item.ranked_case_ids[:10]
        relevant_ranks = [
            rank for rank, case_id in enumerate(ranked, start=1) if case_id in accepted
        ]
        if relevant_ranks:
            recalled += 1
            reciprocal_ranks += 1.0 / relevant_ranks[0]
            if accepted.intersection(ranked).intersection(item.evidence_case_ids):
                hits_with_evidence += 1
        dcg = sum(1.0 / math.log2(rank + 1) for rank in relevant_ranks)
        ideal_count = min(len(accepted), 10)
        ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg += 0.0 if ideal == 0 else dcg / ideal
    return (
        recalled / len(items),
        reciprocal_ranks / len(items),
        ndcg / len(items),
        1.0 if recalled == 0 else hits_with_evidence / recalled,
    )


def evaluate_retrieval(system: object, observations: object) -> RetrievalMetrics:
    if (
        type(system) is not str
        or system not in {"substring", "lexical", "dense", "hybrid"}
        or type(observations) is not tuple
        or not observations
        or len(observations) > _MAX_OBSERVATIONS
    ):
        _raise("retrieval_metrics_invalid")
    checked = tuple(
        _revalidate(item) for item in cast(tuple[object, ...], observations)
    )
    if any(item is None for item in checked):
        _raise("retrieval_metrics_invalid")
    items = cast(tuple[RetrievalObservation, ...], checked)
    if len({item.item_id for item in items}) != len(items):
        _raise("retrieval_metrics_invalid")
    positives = tuple(item for item in items if not item.no_answer_expected)
    no_answers = tuple(item for item in items if item.no_answer_expected)
    recall, mrr, ndcg, evidence = _positive_metrics(positives)
    yearly = tuple(
        (
            year,
            _positive_metrics(
                tuple(item for item in positives if item.edition_year == year)
            )[0],
        )
        for year in range(2020, 2026)
    )
    ocr_slices: list[tuple[str, float]] = []
    for year in (2023, 2024, 2025):
        for quality in ("low_resolution", "high_resolution"):
            selected = tuple(
                item
                for item in positives
                if item.edition_year == year and item.ocr_quality_group == quality
            )
            if selected:
                ocr_slices.append((f"{year}:{quality}", _positive_metrics(selected)[0]))
    latencies = tuple(item.latency for item in items)
    warm = LatencyP95(
        normalization_ms=cast(
            float, _p95(tuple(item.normalization_ms for item in latencies))
        ),
        lexical_ms=cast(float, _p95(tuple(item.lexical_ms for item in latencies))),
        dense_ms=cast(float, _p95(tuple(item.dense_ms for item in latencies))),
        fusion_ms=cast(float, _p95(tuple(item.fusion_ms for item in latencies))),
        parent_expansion_ms=cast(
            float, _p95(tuple(item.parent_expansion_ms for item in latencies))
        ),
        total_ms=cast(float, _p95(tuple(item.total_ms for item in latencies))),
    )
    cold = tuple(
        item.cold_start_ms for item in latencies if item.cold_start_ms is not None
    )
    return RetrievalMetrics(
        system=cast(SearchSystem, system),
        observations=len(items),
        positive_questions=len(positives),
        no_answer_questions=len(no_answers),
        recall_at_10=recall,
        recall_at_10_by_year=yearly,
        mrr_at_10=mrr,
        ndcg_at_10=ndcg,
        evidence_span_coverage=evidence,
        no_answer_recall=(
            0.0
            if not no_answers
            else sum(item.no_answer_candidate for item in no_answers) / len(no_answers)
        ),
        ocr_recall_at_10=tuple(ocr_slices),
        warm_latency_p95_ms=warm,
        cold_start_p95_ms=_p95(cold),
    )


def retrieval_release_ready(value: object) -> bool:
    if type(value) is not RetrievalMetrics:
        return False
    report = value
    return (
        report.system == "hybrid"
        and report.positive_questions > 0
        and report.no_answer_questions > 0
        and report.recall_at_10 >= 0.95
        and all(score >= 0.90 for _, score in report.recall_at_10_by_year)
        and report.mrr_at_10 >= 0.75
        and report.ndcg_at_10 >= 0.80
        and report.evidence_span_coverage >= 0.98
        and report.no_answer_recall >= 0.95
        and report.warm_latency_p95_ms.total_ms <= 3_000.0
    )
