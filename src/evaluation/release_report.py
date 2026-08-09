"""Gold-bound, aggregate-only release evaluation reports."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from pathlib import Path
from typing import Literal, NoReturn, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.corpus.ids import validate_case_id
from src.corpus.storage import StorageError, connect_canonical_storage
from src.evaluation.goldset import (
    EvidenceRequirement,
    ReleaseGoldItem,
    load_release_goldsets,
)
from src.evaluation.ingestion_metrics import (
    IngestionMetricError,
    IngestionMetrics,
    IngestionObservation,
    evaluate_ingestion,
    ingestion_release_ready,
)
from src.evaluation.retrieval_metrics import (
    RetrievalMetricError,
    RetrievalMetrics,
    RetrievalObservation,
    evaluate_retrieval,
    retrieval_release_ready,
)

_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_SYSTEMS = ("substring", "lexical", "dense", "hybrid")
_MAX_OBSERVATION_BYTES = 16 * 1024 * 1024
_MAX_OBSERVATION_LINE_BYTES = 64 * 1024
_MAX_CANONICAL_CASES = 1_000_000
_ObservationT = TypeVar("_ObservationT", IngestionObservation, RetrievalObservation)


class ReleaseReportError(ValueError):
    """A fixed, value-free evaluation report failure."""


def _raise(code: str) -> NoReturn:
    raise ReleaseReportError(code) from None


class _ReportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class IngestionAggregate(_ReportModel):
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


class LatencyAggregate(_ReportModel):
    normalization_ms: float
    lexical_ms: float
    dense_ms: float
    fusion_ms: float
    parent_expansion_ms: float
    total_ms: float


class RetrievalAggregate(_ReportModel):
    system: Literal["substring", "lexical", "dense", "hybrid"]
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
    warm_latency_p95_ms: LatencyAggregate
    cold_start_p95_ms: float | None


class ReleaseEvaluationReport(_ReportModel):
    schema_version: Literal["sen-qa-release-evaluation/v1"] = (
        "sen-qa-release-evaluation/v1"
    )
    release_id: str = Field(pattern=r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
    gold_items: Literal[200]
    blind_items: Literal[60]
    ingestion_gate: bool
    retrieval_gate: bool
    ingestion: IngestionAggregate
    retrieval: tuple[RetrievalAggregate, ...]


def _fields(value: object, model_type: type[BaseModel]) -> dict[str, object] | None:
    if type(value) is not model_type:
        return None
    try:
        raw = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(raw) is not dict or set(raw) != set(model_type.model_fields):
        return None
    return dict(raw)


def _revalidate_gold(value: object) -> ReleaseGoldItem | None:
    raw = _fields(value, ReleaseGoldItem)
    if raw is None or type(raw.get("required_evidence")) is not tuple:
        return None
    evidence: list[dict[str, object]] = []
    for item in cast(tuple[object, ...], raw["required_evidence"]):
        checked = _fields(item, EvidenceRequirement)
        if checked is None:
            return None
        evidence.append(checked)
    raw["required_evidence"] = tuple(evidence)
    try:
        return ReleaseGoldItem.model_validate(raw)
    except (ValidationError, TypeError, ValueError):
        return None


def _read_private_regular_file(path: object) -> bytes | None:
    if not isinstance(path, Path):
        return None
    absolute = Path(os.path.abspath(os.fspath(path)))
    if len(absolute.parts) < 2:
        return None
    directory_fd = -1
    descriptor = -1
    failed = False
    data = b""
    try:
        directory_fd = os.open(
            absolute.parts[0],
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        for component in absolute.parts[1:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            absolute.parts[-1],
            os.O_RDONLY
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or bool(before.st_mode & 0o077)
            or before.st_size <= 0
            or before.st_size > _MAX_OBSERVATION_BYTES
        ):
            failed = True
        else:
            remaining = before.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    failed = True
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if os.read(descriptor, 1) or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                failed = True
    except OSError:
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                failed = True
    return None if failed else data


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite number")


def _load_observations(
    path: object, model_type: type[_ObservationT]
) -> tuple[_ObservationT, ...]:
    data = _read_private_regular_file(path)
    if data is None or not data.endswith(b"\n"):
        _raise("evaluation_observations_invalid")
    lines = data.splitlines()
    if len(lines) != 200 or any(
        not line or len(line) > _MAX_OBSERVATION_LINE_BYTES for line in lines
    ):
        _raise("evaluation_observations_invalid")
    records: list[_ObservationT] = []
    failed = False
    try:
        for line in lines:
            payload = json.loads(
                line.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if type(payload) is not dict:
                failed = True
                break
            records.append(model_type.model_validate_json(line))
    except (UnicodeError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        failed = True
    if failed or len(records) != 200:
        _raise("evaluation_observations_invalid")
    return tuple(records)


def load_ingestion_observations(path: Path) -> tuple[IngestionObservation, ...]:
    """Load exactly 200 owner-only ingestion observations."""
    return _load_observations(path, IngestionObservation)


def load_retrieval_observations(
    paths: object,
) -> dict[str, tuple[RetrievalObservation, ...]]:
    """Load one exact owner-only 200-record observation set per search system."""
    if type(paths) is not dict or set(paths) != set(_SYSTEMS):
        _raise("evaluation_observations_invalid")
    result: dict[str, tuple[RetrievalObservation, ...]] = {}
    for system in _SYSTEMS:
        path = cast(dict[object, object], paths)[system]
        if not isinstance(path, Path):
            _raise("evaluation_observations_invalid")
        result[system] = _load_observations(path, RetrievalObservation)
    return result


def load_canonical_evidence(
    database: Path,
) -> dict[str, frozenset[tuple[int, int]]]:
    """Read locatable evidence only for canonical searchable cases."""
    result: dict[str, set[tuple[int, int]]] = {}
    searchable: set[str] = set()
    failed = False
    try:
        with connect_canonical_storage(database) as connection:
            case_count = connection.execute(
                "SELECT count(*) FROM cases WHERE search_eligible=1"
            ).fetchone()
            if (
                type(case_count) is not tuple
                or len(case_count) != 1
                or type(case_count[0]) is not int
                or case_count[0] < 1
                or case_count[0] > _MAX_CANONICAL_CASES
            ):
                failed = True
            if not failed:
                case_rows = connection.execute(
                    "SELECT case_id,review_status,search_eligible FROM cases "
                    "WHERE search_eligible=1 ORDER BY case_id"
                ).fetchall()
                for case_id, review_status, search_eligible in case_rows:
                    if (
                        type(case_id) is not str
                        or type(review_status) is not str
                        or type(search_eligible) is not int
                        or review_status not in {"search_approved", "approved"}
                        or search_eligible != 1
                    ):
                        failed = True
                        break
                    validate_case_id(case_id)
                    searchable.add(case_id)
                    result[case_id] = set()
            if not failed:
                span_rows = connection.execute(
                    "SELECT s.case_id,s.span_index,s.pdf_page_index "
                    "FROM source_spans AS s JOIN cases AS c ON c.case_id=s.case_id "
                    "WHERE c.search_eligible=1 ORDER BY s.case_id,s.span_index"
                ).fetchall()
                if len(span_rows) > _MAX_CANONICAL_CASES:
                    failed = True
                for case_id, span_index, page_index in span_rows:
                    if (
                        case_id not in searchable
                        or type(span_index) is not int
                        or type(page_index) is not int
                        or span_index < 0
                        or span_index > 1_000_000
                        or page_index < 1
                        or page_index > 10_000
                        or (page_index, span_index) in result[case_id]
                    ):
                        failed = True
                        break
                    result[case_id].add((page_index, span_index))
                if any(not references for references in result.values()):
                    failed = True
    except (StorageError, sqlite3.Error, TypeError, ValueError):
        failed = True
    if failed:
        _raise("canonical_evidence_invalid")
    return {
        case_id: frozenset(references) for case_id, references in sorted(result.items())
    }


def _ingestion_payload(metrics: IngestionMetrics) -> dict[str, object]:
    return {
        name: object.__getattribute__(metrics, name)
        for name in IngestionAggregate.model_fields
    }


def _retrieval_payload(metrics: RetrievalMetrics) -> dict[str, object]:
    latency = metrics.warm_latency_p95_ms
    return {
        "system": metrics.system,
        "observations": metrics.observations,
        "positive_questions": metrics.positive_questions,
        "no_answer_questions": metrics.no_answer_questions,
        "recall_at_10": metrics.recall_at_10,
        "recall_at_10_by_year": metrics.recall_at_10_by_year,
        "mrr_at_10": metrics.mrr_at_10,
        "ndcg_at_10": metrics.ndcg_at_10,
        "evidence_span_coverage": metrics.evidence_span_coverage,
        "no_answer_recall": metrics.no_answer_recall,
        "ocr_recall_at_10": metrics.ocr_recall_at_10,
        "warm_latency_p95_ms": {
            "normalization_ms": latency.normalization_ms,
            "lexical_ms": latency.lexical_ms,
            "dense_ms": latency.dense_ms,
            "fusion_ms": latency.fusion_ms,
            "parent_expansion_ms": latency.parent_expansion_ms,
            "total_ms": latency.total_ms,
        },
        "cold_start_p95_ms": metrics.cold_start_p95_ms,
    }


def _expected_ocr_group(item: ReleaseGoldItem) -> str:
    if item.edition_year <= 2022:
        return "none"
    return "low_resolution" if item.low_resolution_ocr else "high_resolution"


def build_release_evaluation_report(
    *,
    release_id: str,
    gold_items: object,
    ingestion_observations: object,
    retrieval_observations: object,
) -> ReleaseEvaluationReport:
    """Bind observations to reviewed gold labels and retain aggregates only."""
    if (
        type(release_id) is not str
        or _RELEASE_RE.fullmatch(release_id) is None
        or type(gold_items) is not tuple
        or len(gold_items) != 200
        or type(ingestion_observations) is not tuple
        or len(ingestion_observations) != 200
        or type(retrieval_observations) is not dict
        or set(retrieval_observations) != set(_SYSTEMS)
    ):
        _raise("evaluation_observations_invalid")
    checked_gold = tuple(
        _revalidate_gold(item) for item in cast(tuple[object, ...], gold_items)
    )
    if any(item is None for item in checked_gold):
        _raise("evaluation_observations_invalid")
    gold = cast(tuple[ReleaseGoldItem, ...], checked_gold)
    gold_by_id = {item.item_id: item for item in gold}
    if (
        len(gold_by_id) != 200
        or sum(item.item_id.startswith("eval-blind-") for item in gold) != 60
    ):
        _raise("evaluation_observations_invalid")

    ingestion_by_id: dict[str, IngestionObservation] = {}
    for observation in cast(tuple[object, ...], ingestion_observations):
        if type(observation) is not IngestionObservation:
            _raise("evaluation_observations_invalid")
        item = gold_by_id.get(observation.item_id)
        if (
            item is None
            or observation.gold_case_ids != item.accepted_case_ids
            or observation.blind != item.item_id.startswith("eval-blind-")
            or observation.item_id in ingestion_by_id
        ):
            _raise("evaluation_observations_invalid")
        ingestion_by_id[observation.item_id] = observation
    if set(ingestion_by_id) != set(gold_by_id):
        _raise("evaluation_observations_invalid")

    checked_systems: dict[str, tuple[RetrievalObservation, ...]] = {}
    for system in _SYSTEMS:
        raw_observations = cast(dict[object, object], retrieval_observations).get(
            system
        )
        if type(raw_observations) is not tuple or len(raw_observations) != 200:
            _raise("evaluation_observations_invalid")
        by_id: dict[str, RetrievalObservation] = {}
        for observation in cast(tuple[object, ...], raw_observations):
            if type(observation) is not RetrievalObservation:
                _raise("evaluation_observations_invalid")
            item = gold_by_id.get(observation.item_id)
            if (
                item is None
                or observation.item_id in by_id
                or observation.edition_year != item.edition_year
                or observation.accepted_case_ids != item.accepted_case_ids
                or observation.no_answer_expected != item.no_answer
                or observation.ocr_quality_group != _expected_ocr_group(item)
            ):
                _raise("evaluation_observations_invalid")
            by_id[observation.item_id] = observation
        if set(by_id) != set(gold_by_id):
            _raise("evaluation_observations_invalid")
        checked_systems[system] = tuple(by_id[item.item_id] for item in gold)

    try:
        ingestion_metrics = evaluate_ingestion(
            tuple(ingestion_by_id[item.item_id] for item in gold)
        )
        retrieval_metrics = tuple(
            evaluate_retrieval(system, checked_systems[system]) for system in _SYSTEMS
        )
        return ReleaseEvaluationReport(
            release_id=release_id,
            gold_items=200,
            blind_items=60,
            ingestion_gate=ingestion_release_ready(ingestion_metrics),
            retrieval_gate=retrieval_release_ready(retrieval_metrics[-1]),
            ingestion=IngestionAggregate.model_validate(
                _ingestion_payload(ingestion_metrics)
            ),
            retrieval=tuple(
                RetrievalAggregate.model_validate(_retrieval_payload(metrics))
                for metrics in retrieval_metrics
            ),
        )
    except (
        IngestionMetricError,
        RetrievalMetricError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        _raise("evaluation_observations_invalid")


def canonical_release_evaluation_bytes(report: object) -> bytes:
    """Render canonical aggregate-only evaluation JSON."""
    raw = _fields(report, ReleaseEvaluationReport)
    if raw is None:
        _raise("evaluation_report_invalid")
    try:
        checked = ReleaseEvaluationReport.model_validate(raw)
        return (
            json.dumps(
                checked.model_dump(mode="json"),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )
    except (ValidationError, TypeError, ValueError):
        _raise("evaluation_report_invalid")


def _open_parent(path: Path) -> tuple[int, str] | None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if len(absolute.parts) < 2 or absolute.name in {"", ".", ".."}:
        return None
    descriptor = -1
    failed = False
    try:
        descriptor = os.open(
            absolute.parts[0],
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        for component in absolute.parts[1:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_fd
    except OSError:
        failed = True
    if failed:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        return None
    return descriptor, absolute.name


def _write_new_report(path: object, payload: bytes) -> None:
    if not isinstance(path, Path) or not payload or len(payload) > 1024 * 1024:
        _raise("evaluation_report_write_failed")
    opened = _open_parent(path)
    if opened is None:
        _raise("evaluation_report_write_failed")
    directory_fd, leaf = opened
    temporary = f".{leaf}.{os.urandom(12).hex()}"
    descriptor = -1
    failed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                failed = True
                break
            written += count
        if not failed:
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.link(
                temporary,
                leaf,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.fsync(directory_fd)
    except OSError:
        failed = True
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        except OSError:
            failed = True
        try:
            os.close(directory_fd)
        except OSError:
            failed = True
    if failed:
        _raise("evaluation_report_write_failed")


def create_release_evaluation_report(
    *,
    release_id: str,
    canonical_database: Path,
    dev_gold: Path,
    blind_gold: Path,
    blind_labels: Path,
    ingestion_path: Path,
    retrieval_paths: object,
    output: Path,
) -> ReleaseEvaluationReport:
    """Evaluate exact on-disk release evidence and publish aggregates only."""
    canonical_evidence = load_canonical_evidence(canonical_database)
    gold = load_release_goldsets(
        dev_gold,
        blind_gold,
        blind_labels,
        canonical_evidence=canonical_evidence,
    )
    report = build_release_evaluation_report(
        release_id=release_id,
        gold_items=gold,
        ingestion_observations=load_ingestion_observations(ingestion_path),
        retrieval_observations=load_retrieval_observations(retrieval_paths),
    )
    _write_new_report(output, canonical_release_evaluation_bytes(report))
    return report
