"""Offline OCR extraction for the approved 2023-2025 PDFs."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Final, Literal, NamedTuple, Protocol, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.ingestion.extract_common import (
    APPROVED_LAYOUT_DETECTOR_VERSION,
    PAGE_RECORD_SCHEMA_VERSION,
    BoundingBox,
    LayoutEvidence,
    LayoutRegion,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
    SemanticHint,
    exact_declared_field_mapping,
    printed_page_label,
    revalidate_raw_page,
    revalidate_source_document,
)
from src.ingestion.extract_native import DOCUMENT_IO_ERRORS, PAGE_EXTRACTION_ERRORS
from src.ingestion.manifest import SourceDocument
from src.ingestion.policy import OCR_LOW_CONFIDENCE_THRESHOLD

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PACKAGE_VERSIONS = {"paddleocr": "3.7.0", "paddlepaddle": "3.1.1"}
_OFFICIAL_MODEL_HOST = "paddle-model-ecology.bj.bcebos.com"
_MUTABLE_REVISIONS = {"latest", "main", "master", "head"}
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCUMENT_NUMBER_RE = re.compile(r"^[0-9A-Za-z가-힣\s-]+$")
_OCR_RENDER_HASH_PREFIX = b"sen-qa-ocr-render-v1\0rgb8\0"
_LAYOUT_SEGMENT_REGISTRY_HASH_PREFIX = b"sen-qa-layout-segment-registry-v1\0"
_LAYOUT_SEGMENT_ID_HASH_PREFIX = b"sen-qa-layout-segment-id-v1\0"
LAYOUT_SEGMENT_REGISTRY_POLICY_VERSION: Final = "layout-segment-registry-v1"
FieldType = SemanticHint
CriticalFieldType = Literal[
    "title", "question", "amount", "date", "law_name", "article"
]
_CRITICAL_FIELD_TYPES: tuple[CriticalFieldType, ...] = (
    "title",
    "question",
    "amount",
    "date",
    "law_name",
    "article",
)


class LayoutSegmentRegistryEntry(NamedTuple):
    """One checked-in approved document-body segment."""

    doc_id: str
    source_sha256: str
    segment_start_pdf_page: int
    segment_end_pdf_page: int


APPROVED_LAYOUT_SEGMENT_REGISTRY: Final[Mapping[int, LayoutSegmentRegistryEntry]] = (
    MappingProxyType(
        {
            2024: LayoutSegmentRegistryEntry(
                doc_id="sen-qa-2024",
                source_sha256="fc1494eff8ee3fe9b53606dd5f55468d8ec254b9d2d661fba6c5e4b46daa99ed",
                segment_start_pdf_page=7,
                segment_end_pdf_page=323,
            ),
            2025: LayoutSegmentRegistryEntry(
                doc_id="sen-qa-2025",
                source_sha256="9a1a7b0ebf1346b540c97d9990dd3b43c647ce397322ff0fabe6d2de84c0ce03",
                segment_start_pdf_page=7,
                segment_end_pdf_page=313,
            ),
        }
    )
)


class ModelLockError(Exception):
    """Raised when locked model metadata or installed bytes are untrusted."""


class LockedPackages(BaseModel):
    """Exact OCR runtime package versions frozen by uv.lock."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    paddleocr: str
    paddlepaddle: str


class LockedModelFile(BaseModel):
    """One required file in an extracted inference model."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    sha256: str


class LockedModel(BaseModel):
    """One immutable official archive and its required extracted files."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    revision: str
    source_url: str
    archive_sha256: str
    files: tuple[LockedModelFile, ...]


class ModelLock(BaseModel):
    """Complete local-only Paddle model contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int
    language: str
    packages: LockedPackages
    models: tuple[LockedModel, ...]


class OcrAdapterError(Exception):
    """Expected OCR engine failure safe to quarantine at a page boundary."""


class OcrExtractionError(Exception):
    """Sanitized document-level OCR extraction failure."""


class LayoutDetectionError(Exception):
    """Expected raster-layout failure whose source detail must not escape."""


class RasterImage(NamedTuple):
    """Dependency-free RGB raster passed to an injected OCR adapter."""

    width: int
    height: int
    rgb_bytes: bytes


class RasterLayoutRegion(BaseModel):
    """One detector result in half-open rendered-pixel coordinates."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    region_type: Literal["card"] = "card"
    bbox: tuple[float, float, float, float]
    evidence: Literal["raster-border"] = "raster-border"

    @model_validator(mode="after")
    def has_finite_positive_geometry(self) -> RasterLayoutRegion:
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(value) for value in self.bbox) or x0 >= x1 or y0 >= y1:
            raise ValueError("raster layout region must have finite positive geometry")
        return self


class LayoutDetector(Protocol):
    """Injectable complete-page detector used independently of OCR recognition."""

    version: str

    def detect(self, image: RasterImage) -> tuple[RasterLayoutRegion, ...]:
        """Return deterministically ordered regions without recognized text."""


class GreenCardBorderDetector:
    """Detect paired thin green horizontal borders without OCR or source text."""

    __slots__ = ()

    version = APPROVED_LAYOUT_DETECTOR_VERSION
    _MIN_ROW_DENSITY = 0.45
    _MIN_BORDER_WIDTH = 0.70
    _MAX_BORDER_THICKNESS = 0.012
    _MIN_CARD_HEIGHT = 0.10
    _MAX_CARD_HEIGHT = 0.30
    _MAX_X_EDGE_DRIFT = 0.08

    def detect(self, image: RasterImage) -> tuple[RasterLayoutRegion, ...]:
        expected_size = image.width * image.height * 3
        if (
            image.width <= 0
            or image.height <= 0
            or len(image.rgb_bytes) != expected_size
        ):
            raise LayoutDetectionError("raster layout input is invalid")
        samples = memoryview(image.rgb_bytes)
        horizontal_step = max(1, image.width // 1200)
        sampled_columns = len(range(0, image.width, horizontal_step))
        minimum_green = max(1, math.ceil(sampled_columns * self._MIN_ROW_DENSITY))
        dense_rows: list[tuple[int, int, int]] = []
        for y in range(image.height):
            best_count = 0
            best_x0: int | None = None
            best_x1: int | None = None
            run_count = 0
            run_x0: int | None = None
            last_green_x: int | None = None
            row_offset = y * image.width * 3
            for x in range(0, image.width, horizontal_step):
                offset = row_offset + x * 3
                red = samples[offset]
                green = samples[offset + 1]
                blue = samples[offset + 2]
                if green >= 80 and green >= red + 12 and green >= blue + 8:
                    if (
                        run_x0 is None
                        or last_green_x is None
                        or x > last_green_x + horizontal_step * 2
                    ):
                        run_count = 0
                        run_x0 = x
                    run_count += 1
                    last_green_x = x
                    if run_count > best_count:
                        best_count = run_count
                        best_x0 = run_x0
                        best_x1 = min(image.width, x + horizontal_step)
            if (
                best_count >= minimum_green
                and best_x0 is not None
                and best_x1 is not None
            ):
                dense_rows.append((y, best_x0, best_x1))

        runs: list[tuple[int, int, int, int]] = []
        for y, x0, x1 in dense_rows:
            if not runs or y != runs[-1][1]:
                runs.append((y, y + 1, x0, x1))
            else:
                start, _, run_x0, run_x1 = runs[-1]
                runs[-1] = (start, y + 1, min(run_x0, x0), max(run_x1, x1))

        max_thickness = max(1, math.ceil(image.height * self._MAX_BORDER_THICKNESS))
        thin_runs = [
            run
            for run in runs
            if run[1] - run[0] <= max_thickness
            and run[3] - run[2] >= image.width * self._MIN_BORDER_WIDTH
        ]
        minimum_height = image.height * self._MIN_CARD_HEIGHT
        maximum_height = image.height * self._MAX_CARD_HEIGHT
        maximum_edge_drift = image.width * self._MAX_X_EDGE_DRIFT
        regions: list[RasterLayoutRegion] = []
        index = 0
        while index + 1 < len(thin_runs):
            top = thin_runs[index]
            bottom = thin_runs[index + 1]
            height = ((bottom[0] + bottom[1]) - (top[0] + top[1])) / 2.0
            intervening_thick_run = any(
                run[0] >= top[1]
                and run[1] <= bottom[0]
                and run[1] - run[0] > max_thickness
                for run in runs
            )
            matching_edges = (
                abs(top[2] - bottom[2]) <= maximum_edge_drift
                and abs(top[3] - bottom[3]) <= maximum_edge_drift
            )
            if (
                minimum_height <= height <= maximum_height
                and matching_edges
                and not intervening_thick_run
            ):
                regions.append(
                    RasterLayoutRegion(
                        bbox=(
                            float(min(top[2], bottom[2])),
                            float(top[0]),
                            float(max(top[3], bottom[3])),
                            float(bottom[1]),
                        )
                    )
                )
                index += 2
            else:
                index += 1
        return tuple(regions)


class AdapterLine(BaseModel):
    """One immutable adapter result in rendered-image pixel coordinates."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float = Field(ge=0.0, le=1.0)
    field_type: FieldType = "ocr_line"

    @model_validator(mode="after")
    def has_valid_geometry_and_confidence(self) -> AdapterLine:
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(value) for value in self.bbox) or x0 > x1 or y0 > y1:
            raise ValueError("adapter bbox must be finite and ordered")
        if not math.isfinite(self.confidence):
            raise ValueError("adapter confidence must be finite")
        return self


class OcrAdapter(Protocol):
    """Host-testable boundary implemented by the runtime Paddle adapter."""

    def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
        """Return measured OCR lines for a complete rendered page."""


def _infer_field_type(text: str) -> FieldType:
    compact = text.strip()
    if "문서번호" in compact or re.match(r"^[가-힣]+\s*\d{4}-\d+", compact):
        return "document_number"
    if compact.startswith("질문"):
        return "question"
    if re.search(r"\d[\d,]*\s*(?:원|만원|천원)", compact):
        return "amount"
    if re.search(r"\d{4}[.\-/년]\s*\d{1,2}", compact):
        return "date"
    if "법" in compact or "령" in compact or "규칙" in compact:
        return "law_name"
    if re.search(r"제\s*\d+\s*조", compact):
        return "article"
    return "ocr_line"


class PaddleOcrAdapter:
    """Runtime-only PaddleOCR adapter; imports occur only after local lock validation."""

    def __init__(self, *, detection_model: Path, recognition_model: Path) -> None:
        try:
            numpy_module = importlib.import_module("numpy")
            self._numpy = numpy_module
            PaddleOCR = importlib.import_module("paddleocr").PaddleOCR
            self._pipeline = PaddleOCR(
                lang="korean",
                text_detection_model_name="PP-OCRv5_server_det",
                text_detection_model_dir=str(detection_model),
                text_recognition_model_name="korean_PP-OCRv5_mobile_rec",
                text_recognition_model_dir=str(recognition_model),
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="cpu",
            )
        except Exception as error:
            raise OcrAdapterError(
                "cannot initialize locked local OCR adapter"
            ) from error

    def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
        try:
            array = (
                self._numpy.frombuffer(image.rgb_bytes, dtype=self._numpy.uint8)
                .reshape((image.height, image.width, 3))[:, :, ::-1]
                .copy()
            )
            output: list[AdapterLine] = []
            for result in self._pipeline.predict(array):
                payload = result.json
                data = payload.get("res", payload)
                texts = data["rec_texts"]
                scores = data["rec_scores"]
                polygons = data.get("rec_polys", data.get("rec_boxes"))
                if polygons is None or not (len(texts) == len(scores) == len(polygons)):
                    raise ValueError("incomplete Paddle OCR result")
                for text, score, polygon in zip(texts, scores, polygons, strict=True):
                    points = self._numpy.asarray(polygon, dtype=float)
                    if points.ndim == 1 and points.shape[0] == 4:
                        bbox = tuple(float(value) for value in points)
                    else:
                        bbox = (
                            float(points[:, 0].min()),
                            float(points[:, 1].min()),
                            float(points[:, 0].max()),
                            float(points[:, 1].max()),
                        )
                    output.append(
                        AdapterLine(
                            text=str(text),
                            bbox=bbox,  # type: ignore[arg-type]
                            confidence=float(score),
                            field_type=_infer_field_type(str(text)),
                        )
                    )
            return tuple(output)
        except Exception as error:
            raise OcrAdapterError("locked local OCR inference failed") from error


def create_paddle_adapter(lock: ModelLock, model_root: Path) -> OcrAdapter:
    """Validate all local bytes before taking the only Paddle import path."""
    validate_installed_models(lock, model_root)
    by_name = {model.name: model_root / model.name for model in lock.models}
    try:
        detection_model = by_name["PP-OCRv5_server_det_infer"]
        recognition_model = by_name["korean_PP-OCRv5_mobile_rec_infer"]
    except KeyError as error:
        raise ModelLockError(
            "locked detector and Korean recognizer are required"
        ) from error
    return PaddleOcrAdapter(
        detection_model=detection_model, recognition_model=recognition_model
    )


class ReviewEntry(BaseModel):
    """Privacy-safe pointer to a field requiring human review."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    location_id: str = Field(pattern=r"^loc-[0-9a-f]{32}$")
    field_type: FieldType
    confidence: float = Field(ge=0.0, le=1.0)
    reason: Literal["low-confidence", "invalid-document-number"]


class CriticalFieldStatus(BaseModel):
    """A parser-blocking critical-field review marker."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    field_type: CriticalFieldType
    status: Literal["unverified", "sampling_required"]
    review_required: bool


class LayoutSegmentProvenance(BaseModel):
    """Stable text-free grouping used by the annual layout review policy."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    segment_id: str = Field(pattern=r"^layout-segment-[0-9a-f]{32}$")
    segment_key: Literal["approved-document-body"]
    segment_start_pdf_page: int = Field(ge=1)
    segment_end_pdf_page: int = Field(ge=1)
    registry_policy_version: Literal["layout-segment-registry-v1"]
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detector_version: Literal["green-card-border-v1"]
    region_count: int = Field(ge=0)
    sampling_status: Literal["all_cases_required", "sampling_required"]

    @model_validator(mode="after")
    def has_ordered_registry_range(self) -> LayoutSegmentProvenance:
        if self.segment_start_pdf_page > self.segment_end_pdf_page:
            raise ValueError("layout segment registry range must be ordered")
        return self


class ExtractedOcrPageRecord(BaseModel):
    """Successful OCR page with immutable source, raster, and review provenance."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_version: Literal[2]
    status: Literal["extracted"] = "extracted"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_dpi: int = Field(gt=0)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    quality_flags: tuple[str, ...]
    raw_page: RawPage
    layout_segment_provenance: LayoutSegmentProvenance | None
    review_queue: tuple[ReviewEntry, ...]
    critical_review_policy: Literal[
        "all-fields-human-verification", "stratified-sample-with-layout-escalation"
    ]
    critical_fields: tuple[CriticalFieldStatus, ...]
    review_status: Literal["needs_review", "machine_extracted"]
    search_eligible: Literal[False] = False
    answer_eligible: Literal[False] = False

    @model_validator(mode="after")
    def has_matching_raw_provenance(self) -> ExtractedOcrPageRecord:
        policy = ocr_policy(self.edition_year)
        if (
            self.render_dpi != policy.render_dpi
            or self.quality_flags != policy.quality_flags
        ):
            raise ValueError("OCR run provenance does not match edition policy")
        if (
            self.doc_id != self.raw_page.doc_id
            or self.edition_year != self.raw_page.edition_year
            or self.pdf_page_index != self.raw_page.pdf_page_index
            or self.page_label != self.raw_page.page_label
            or self.render_sha256 != self.raw_page.render_sha256
            or self.raw_page.extraction_source != "ocr"
        ):
            raise ValueError("OCR page envelope does not match raw provenance")
        if self.edition_year == 2023:
            if self.raw_page.layout_evidence.status != "not_applicable":
                raise ValueError(
                    "OCR page layout evidence does not match edition policy"
                )
        elif self.raw_page.layout_evidence.status == "not_applicable":
            raise ValueError("OCR page layout evidence does not match edition policy")
        evidence = self.raw_page.layout_evidence
        if evidence.status in {"failed", "not_detected", "detected"} and (
            evidence.detector_version != APPROVED_LAYOUT_DETECTOR_VERSION
        ):
            raise ValueError("OCR page layout detector is not approved")
        registry_entry = APPROVED_LAYOUT_SEGMENT_REGISTRY.get(self.edition_year)
        expected_segment = (
            _layout_segment_provenance(
                self.edition_year,
                self.raw_page,
                source_sha256=self.source_sha256,
                segment_start_pdf_page=registry_entry.segment_start_pdf_page,
                segment_end_pdf_page=registry_entry.segment_end_pdf_page,
            )
            if registry_entry is not None
            else None
        )
        if self.layout_segment_provenance != expected_segment:
            raise ValueError(
                "OCR layout segment provenance does not match raw evidence"
            )
        (
            expected_critical_policy,
            expected_critical_fields,
            expected_review_status,
        ) = _critical_review(
            self.edition_year,
            evidence,
            registry_segment_available=expected_segment is not None,
        )
        if (
            self.critical_review_policy != expected_critical_policy
            or self.critical_fields != expected_critical_fields
            or self.review_status != expected_review_status
        ):
            raise ValueError(
                "OCR critical review status does not match fail-closed policy"
            )
        expected_review_queue = _review_queue_from_raw_page(
            self.raw_page,
            source_sha256=self.source_sha256,
        )
        if self.review_queue != expected_review_queue:
            raise ValueError("OCR review queue does not match raw provenance")
        return self


class QuarantinedOcrPageRecord(BaseModel):
    """Sanitized failed-page record; recognized text and exception detail are excluded."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    schema_version: Literal[2]
    status: Literal["quarantined"] = "quarantined"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    render_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    render_dpi: int = Field(gt=0)
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    quality_flags: tuple[str, ...]
    reason_code: Literal[
        "page-render-failed", "ocr-adapter-failed", "ocr-provenance-invalid"
    ]

    @model_validator(mode="after")
    def matches_edition_policy(self) -> QuarantinedOcrPageRecord:
        policy = ocr_policy(self.edition_year)
        if (
            self.render_dpi != policy.render_dpi
            or self.quality_flags != policy.quality_flags
        ):
            raise ValueError("OCR run provenance does not match edition policy")
        if (self.reason_code == "page-render-failed") != (self.render_sha256 is None):
            raise ValueError("OCR quarantine reason does not match render provenance")
        return self


OcrPageRecord: TypeAlias = ExtractedOcrPageRecord | QuarantinedOcrPageRecord


def _revalidate_review_entry(value: object) -> ReviewEntry | None:
    fields = exact_declared_field_mapping(value, ReviewEntry)
    if fields is None:
        return None
    try:
        return ReviewEntry.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_critical_field(value: object) -> CriticalFieldStatus | None:
    fields = exact_declared_field_mapping(value, CriticalFieldStatus)
    if fields is None:
        return None
    try:
        return CriticalFieldStatus.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_layout_segment(
    value: object,
) -> LayoutSegmentProvenance | None:
    fields = exact_declared_field_mapping(value, LayoutSegmentProvenance)
    if fields is None:
        return None
    try:
        return LayoutSegmentProvenance.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _revalidate_ocr_record(value: object) -> OcrPageRecord | None:
    model: type[ExtractedOcrPageRecord | QuarantinedOcrPageRecord]
    if type(value) is ExtractedOcrPageRecord:
        model = ExtractedOcrPageRecord
    elif type(value) is QuarantinedOcrPageRecord:
        model = QuarantinedOcrPageRecord
    else:
        return None
    fields = exact_declared_field_mapping(value, model)
    if fields is None:
        return None
    if model is ExtractedOcrPageRecord:
        raw_page = revalidate_raw_page(fields["raw_page"])
        raw_review_queue = fields["review_queue"]
        raw_critical_fields = fields["critical_fields"]
        raw_segment = fields["layout_segment_provenance"]
        if (
            raw_page is None
            or type(raw_review_queue) is not tuple
            or type(raw_critical_fields) is not tuple
        ):
            return None
        review_queue = tuple(
            _revalidate_review_entry(item) for item in raw_review_queue
        )
        critical_fields = tuple(
            _revalidate_critical_field(item) for item in raw_critical_fields
        )
        if any(item is None for item in review_queue + critical_fields):
            return None
        if raw_segment is None:
            segment = None
        else:
            segment = _revalidate_layout_segment(raw_segment)
            if segment is None:
                return None
        fields["raw_page"] = raw_page
        fields["review_queue"] = tuple(
            item for item in review_queue if item is not None
        )
        fields["critical_fields"] = tuple(
            item for item in critical_fields if item is not None
        )
        fields["layout_segment_provenance"] = segment
    try:
        return model.model_validate(fields)
    except (TypeError, ValueError):
        return None


def validate_ocr_page_record(value: object) -> OcrPageRecord:
    """Return a recursively rebuilt record or raise a value-free boundary error."""
    record = _revalidate_ocr_record(value)
    if record is None:
        raise OcrExtractionError("OCR page record is invalid")
    return record


class OcrPolicy(NamedTuple):
    """Immutable annual raster and review policy."""

    render_dpi: int
    quality_flags: tuple[str, ...]


_OCR_POLICIES = {
    2023: OcrPolicy(render_dpi=300, quality_flags=("source_approx_96dpi",)),
    2024: OcrPolicy(render_dpi=350, quality_flags=("source_150dpi",)),
    2025: OcrPolicy(render_dpi=300, quality_flags=()),
}


def ocr_policy(edition_year: int) -> OcrPolicy:
    """Return the reviewed policy; unsupported years cannot silently default."""
    try:
        return _OCR_POLICIES[edition_year]
    except KeyError as error:
        raise ValueError("OCR is approved only for edition years 2023-2025") from error


def _safe_relative_model_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ModelLockError("locked model file path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ModelLockError("locked model file path is unsafe")
    return path.as_posix()


def _checked_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ModelLockError(
            "locked model SHA-256 must be 64 lowercase hexadecimal characters"
        )
    return value


def validate_model_lock(payload: object) -> ModelLock:
    """Validate the complete lock before any model or network operation."""
    if not isinstance(payload, dict):
        raise ModelLockError("invalid model lock structure")
    required_keys = {"schema_version", "language", "packages", "models"}
    if set(payload) not in (required_keys, required_keys | {"embedding_models"}):
        raise ModelLockError("invalid model lock structure")
    if "embedding_models" in payload and (
        not isinstance(payload["embedding_models"], list)
        or len(payload["embedding_models"]) != 1
        or not isinstance(payload["embedding_models"][0], dict)
    ):
        raise ModelLockError("invalid model lock structure")
    if "embedding_models" in payload:
        from src.corpus.chunking import (
            ChunkingError,
            validate_embedding_model_lock,
        )

        embedding_lock_valid = True
        try:
            validate_embedding_model_lock(payload)
        except ChunkingError:
            embedding_lock_valid = False
        if not embedding_lock_valid:
            raise ModelLockError("invalid embedding model lock structure") from None
    if payload.get("schema_version") != 1:
        raise ModelLockError("model lock schema version must be 1")
    if payload.get("language") != "korean":
        raise ModelLockError("model lock language must be korean")
    if payload.get("packages") != _PACKAGE_VERSIONS:
        raise ModelLockError("model lock package versions do not match frozen runtime")
    models_payload = payload.get("models")
    if not isinstance(models_payload, list) or not models_payload:
        raise ModelLockError("model lock must contain models")

    names: set[str] = set()
    for model_payload in models_payload:
        if not isinstance(model_payload, dict):
            raise ModelLockError("invalid locked model entry")
        name = model_payload.get("name")
        if (
            not isinstance(name, str)
            or _safe_relative_model_path(name) != name
            or "/" in name
        ):
            raise ModelLockError("locked model name is unsafe")
        if name in names:
            raise ModelLockError("duplicate locked model name")
        names.add(name)
        revision = model_payload.get("revision")
        if (
            not isinstance(revision, str)
            or not revision.strip()
            or revision.casefold() in _MUTABLE_REVISIONS
        ):
            raise ModelLockError("locked model revision must be immutable and nonblank")
        source_url = model_payload.get("source_url")
        if not isinstance(source_url, str):
            raise ModelLockError(
                "locked model source must be an official immutable URL"
            )
        parsed_url = urlsplit(source_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != _OFFICIAL_MODEL_HOST
            or parsed_url.query
            or parsed_url.fragment
            or revision not in parsed_url.path
            or not parsed_url.path.endswith(".tar")
        ):
            raise ModelLockError(
                "locked model source must be an official immutable URL"
            )
        _checked_sha256(model_payload.get("archive_sha256"))
        files_payload = model_payload.get("files")
        if not isinstance(files_payload, list) or not files_payload:
            raise ModelLockError("locked model must list required files")
        paths: set[str] = set()
        for file_payload in files_payload:
            if not isinstance(file_payload, dict):
                raise ModelLockError("invalid locked model file")
            path = _safe_relative_model_path(file_payload.get("path"))
            if path in paths:
                raise ModelLockError("duplicate locked model file path")
            paths.add(path)
            _checked_sha256(file_payload.get("sha256"))

    normalized_payload = {key: payload[key] for key in required_keys}
    normalized_payload["models"] = tuple(
        {**model_payload, "files": tuple(model_payload["files"])}
        for model_payload in models_payload
    )
    try:
        return ModelLock.model_validate(normalized_payload)
    except ValidationError:
        raise ModelLockError("invalid model lock structure") from None


def load_model_lock(path: Path) -> ModelLock:
    """Load a model lock without including malformed file content in errors."""
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ModelLockError("cannot parse model lock") from error
    return validate_model_lock(payload)


def validate_installed_models(lock: ModelLock, model_root: Path) -> None:
    """Require every locked local model file to match before runtime imports."""
    for model in lock.models:
        for locked_file in model.files:
            path = model_root / model.name / locked_file.path
            try:
                if not path.is_file():
                    raise ModelLockError("missing locked model file")
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise ModelLockError("cannot read locked model file") from error
            if digest != locked_file.sha256:
                raise ModelLockError("model file SHA-256 mismatch")


def _row_center(line: AdapterLine) -> float:
    return (line.bbox[1] + line.bbox[3]) / 2.0


def _sort_one_column(lines: list[AdapterLine]) -> tuple[AdapterLine, ...]:
    """Group nearby vertical centers into deterministic rows, then sort left-to-right."""
    if not lines:
        return ()
    heights = sorted(line.bbox[3] - line.bbox[1] for line in lines)
    tolerance = max(1.0, heights[len(heights) // 2] * 0.5)
    rows: list[list[AdapterLine]] = []
    row_centers: list[float] = []
    for line in sorted(
        lines, key=lambda item: (_row_center(item), item.bbox[0], item.text)
    ):
        center = _row_center(line)
        if not rows or abs(center - row_centers[-1]) > tolerance:
            rows.append([line])
            row_centers.append(center)
        else:
            rows[-1].append(line)
            row_centers[-1] = sum(_row_center(item) for item in rows[-1]) / len(
                rows[-1]
            )
    return tuple(
        line
        for row in rows
        for line in sorted(
            row, key=lambda item: (item.bbox[0], item.bbox[1], item.text)
        )
    )


def sort_reading_order(
    lines: tuple[AdapterLine, ...], *, page_width: float
) -> tuple[AdapterLine, ...]:
    """Apply the approved explicit left-column-then-right-column reading rule."""
    if not math.isfinite(page_width) or page_width <= 0:
        raise ValueError("page width must be finite and positive")
    midpoint = page_width / 2.0
    left = [line for line in lines if line.bbox[0] < midpoint]
    right = [line for line in lines if line.bbox[0] >= midpoint]
    return _sort_one_column(left) + _sort_one_column(right)


def _scale_lines_to_pdf_points(
    lines: tuple[AdapterLine, ...],
    *,
    page_rect: Any,
    derotation_matrix: Any,
    raster_width: int,
    raster_height: int,
) -> tuple[tuple[AdapterLine, ...], float, float]:
    """Convert rotated raster pixels to local unrotated PDF-point coordinates."""
    if raster_width <= 0 or raster_height <= 0:
        raise ValueError("OCR raster geometry is invalid")
    rect_x0 = float(page_rect.x0)
    rect_y0 = float(page_rect.y0)
    rect_x1 = float(page_rect.x1)
    rect_y1 = float(page_rect.y1)
    rotated_width = rect_x1 - rect_x0
    rotated_height = rect_y1 - rect_y0
    matrix = tuple(float(value) for value in derotation_matrix)
    if len(matrix) != 6:
        raise ValueError("page derotation matrix is invalid")
    a, b, c, d, e, f = matrix

    def derotate(x: float, y: float) -> tuple[float, float]:
        return (x * a + y * c + e, x * b + y * d + f)

    page_corners = tuple(
        derotate(x, y) for x in (rect_x0, rect_x1) for y in (rect_y0, rect_y1)
    )
    page_x0 = min(point[0] for point in page_corners)
    page_y0 = min(point[1] for point in page_corners)
    page_width = max(point[0] for point in page_corners) - page_x0
    page_height = max(point[1] for point in page_corners) - page_y0
    x_scale = rotated_width / raster_width
    y_scale = rotated_height / raster_height

    point_lines: list[AdapterLine] = []
    for line in lines:
        x0, y0, x1, y1 = line.bbox
        if (
            x0 < 0.0
            or y0 < 0.0
            or x0 >= x1
            or y0 >= y1
            or x1 > raster_width
            or y1 > raster_height
        ):
            raise ValueError("OCR line is outside raster geometry")
        rotated_corners = (
            (rect_x0 + x0 * x_scale, rect_y0 + y0 * y_scale),
            (rect_x0 + x0 * x_scale, rect_y0 + y1 * y_scale),
            (rect_x0 + x1 * x_scale, rect_y0 + y0 * y_scale),
            (rect_x0 + x1 * x_scale, rect_y0 + y1 * y_scale),
        )
        page_points = tuple(derotate(x, y) for x, y in rotated_corners)
        point_bbox = (
            min(point[0] for point in page_points) - page_x0,
            min(point[1] for point in page_points) - page_y0,
            max(point[0] for point in page_points) - page_x0,
            max(point[1] for point in page_points) - page_y0,
        )
        if (
            point_bbox[0] < 0.0
            or point_bbox[1] < 0.0
            or point_bbox[0] >= point_bbox[2]
            or point_bbox[1] >= point_bbox[3]
            or point_bbox[2] > page_width
            or point_bbox[3] > page_height
        ):
            raise ValueError("OCR line is outside PDF page geometry")
        point_lines.append(
            AdapterLine(
                text=line.text,
                bbox=point_bbox,
                confidence=line.confidence,
                field_type=line.field_type,
            )
        )
    return tuple(point_lines), page_width, page_height


def _scale_raster_bbox_to_pdf_points(
    bbox: tuple[float, float, float, float],
    *,
    page_rect: Any,
    derotation_matrix: Any,
    raster_width: int,
    raster_height: int,
) -> BoundingBox:
    """Convert one half-open raster rectangle to local PDF-point coordinates."""
    rect_x0 = float(page_rect.x0)
    rect_y0 = float(page_rect.y0)
    rect_x1 = float(page_rect.x1)
    rect_y1 = float(page_rect.y1)
    rotated_width = rect_x1 - rect_x0
    rotated_height = rect_y1 - rect_y0
    matrix = tuple(float(value) for value in derotation_matrix)
    if len(matrix) != 6 or raster_width <= 0 or raster_height <= 0:
        raise ValueError("page layout transform is invalid")
    a, b, c, d, e, f = matrix

    def derotate(x: float, y: float) -> tuple[float, float]:
        return (x * a + y * c + e, x * b + y * d + f)

    page_corners = tuple(
        derotate(x, y) for x in (rect_x0, rect_x1) for y in (rect_y0, rect_y1)
    )
    page_x0 = min(point[0] for point in page_corners)
    page_y0 = min(point[1] for point in page_corners)
    x_scale = rotated_width / raster_width
    y_scale = rotated_height / raster_height
    x0, y0, x1, y1 = bbox
    rotated_corners = (
        (rect_x0 + x0 * x_scale, rect_y0 + y0 * y_scale),
        (rect_x0 + x0 * x_scale, rect_y0 + y1 * y_scale),
        (rect_x0 + x1 * x_scale, rect_y0 + y0 * y_scale),
        (rect_x0 + x1 * x_scale, rect_y0 + y1 * y_scale),
    )
    page_points = tuple(derotate(x, y) for x, y in rotated_corners)
    return BoundingBox(
        x0=min(point[0] for point in page_points) - page_x0,
        y0=min(point[1] for point in page_points) - page_y0,
        x1=max(point[0] for point in page_points) - page_x0,
        y1=max(point[1] for point in page_points) - page_y0,
    )


def _layout_evidence(
    edition_year: int,
    image: RasterImage,
    detector: LayoutDetector | None,
    *,
    page_rect: Any,
    derotation_matrix: Any,
) -> LayoutEvidence:
    """Collect explicit layout evidence without guessing when detection is unavailable."""
    if edition_year == 2023:
        return LayoutEvidence()
    if detector is None:
        return LayoutEvidence(status="unavailable")
    try:
        raster_regions = detector.detect(image)
    except LayoutDetectionError:
        return LayoutEvidence(
            status="failed", detector_version=APPROVED_LAYOUT_DETECTOR_VERSION
        )
    if not isinstance(raster_regions, tuple) or any(
        type(region) is not RasterLayoutRegion for region in raster_regions
    ):
        return LayoutEvidence(
            status="failed", detector_version=APPROVED_LAYOUT_DETECTOR_VERSION
        )
    revalidated_regions = tuple(
        _revalidate_raster_layout_region(region) for region in raster_regions
    )
    if any(region is None for region in revalidated_regions):
        return LayoutEvidence(
            status="failed", detector_version=APPROVED_LAYOUT_DETECTOR_VERSION
        )
    raster_regions = tuple(
        region for region in revalidated_regions if region is not None
    )
    keys = tuple(
        (region.bbox[1], region.bbox[0], region.bbox[3], region.bbox[2])
        for region in raster_regions
    )
    if (
        keys != tuple(sorted(keys))
        or len(keys) != len(set(keys))
        or any(
            not all(math.isfinite(value) for value in region.bbox)
            or region.bbox[0] < 0.0
            or region.bbox[1] < 0.0
            or region.bbox[0] >= region.bbox[2]
            or region.bbox[1] >= region.bbox[3]
            or region.bbox[2] > image.width
            or region.bbox[3] > image.height
            for region in raster_regions
        )
    ):
        return LayoutEvidence(
            status="failed", detector_version=APPROVED_LAYOUT_DETECTOR_VERSION
        )
    if not raster_regions:
        return LayoutEvidence(
            status="not_detected",
            detector_version=APPROVED_LAYOUT_DETECTOR_VERSION,
        )
    point_regions = tuple(
        sorted(
            (
                LayoutRegion(
                    region_type=region.region_type,
                    bbox=_scale_raster_bbox_to_pdf_points(
                        region.bbox,
                        page_rect=page_rect,
                        derotation_matrix=derotation_matrix,
                        raster_width=image.width,
                        raster_height=image.height,
                    ),
                    evidence=region.evidence,
                )
                for region in raster_regions
            ),
            key=lambda region: (
                region.bbox.y0,
                region.bbox.x0,
                region.bbox.y1,
                region.bbox.x1,
                region.region_type,
            ),
        )
    )
    return LayoutEvidence(
        status="detected",
        detector_version=APPROVED_LAYOUT_DETECTOR_VERSION,
        regions=point_regions,
    )


def _revalidate_raster_layout_region(
    value: object,
) -> RasterLayoutRegion | None:
    fields = exact_declared_field_mapping(value, RasterLayoutRegion)
    if fields is None or type(fields["bbox"]) is not tuple:
        return None
    try:
        return RasterLayoutRegion.model_validate(fields)
    except (TypeError, ValueError):
        return None


def _render_hash(pixmap: Any, *, render_dpi: int) -> str:
    header = _OCR_RENDER_HASH_PREFIX + (
        f"{render_dpi}:{int(pixmap.width)}:{int(pixmap.height)}:{int(pixmap.n)}:"
    ).encode("ascii")
    return hashlib.sha256(header + bytes(pixmap.samples)).hexdigest()


def _location_id(*, source_sha256: str, pdf_page_index: int, line: AdapterLine) -> str:
    x0, y0, x1, y1 = line.bbox
    stable = (
        f"{source_sha256}:{pdf_page_index}:{x0:.6f}:{y0:.6f}:{x1:.6f}:{y1:.6f}:"
        f"{line.field_type}"
    )
    return "loc-" + hashlib.sha256(stable.encode("ascii")).hexdigest()[:32]


def _valid_document_number(text: str) -> bool:
    value = re.sub(r"^\s*문서번호\s*[:：]?\s*", "", text)
    return bool(value) and _DOCUMENT_NUMBER_RE.fullmatch(value) is not None


def _review_queue(
    lines: tuple[AdapterLine, ...], *, source_sha256: str, pdf_page_index: int
) -> tuple[ReviewEntry, ...]:
    entries: list[ReviewEntry] = []
    for line in lines:
        reason: Literal["low-confidence", "invalid-document-number"] | None = None
        if line.confidence < OCR_LOW_CONFIDENCE_THRESHOLD:
            reason = "low-confidence"
        elif line.field_type == "document_number" and not _valid_document_number(
            line.text
        ):
            reason = "invalid-document-number"
        if reason is not None:
            entries.append(
                ReviewEntry(
                    location_id=_location_id(
                        source_sha256=source_sha256,
                        pdf_page_index=pdf_page_index,
                        line=line,
                    ),
                    field_type=line.field_type,
                    confidence=line.confidence,
                    reason=reason,
                )
            )
    return tuple(entries)


def _review_queue_from_raw_page(
    raw_page: RawPage, *, source_sha256: str
) -> tuple[ReviewEntry, ...]:
    """Rebuild the value-free review queue from the exact production OCR shape."""
    if len(raw_page.raw_blocks) != 1:
        raise ValueError("OCR raw page shape is invalid")
    page_block = raw_page.raw_blocks[0]
    if page_block.bbox != BoundingBox(
        x0=0.0,
        y0=0.0,
        x1=raw_page.page_width,
        y1=raw_page.page_height,
    ):
        raise ValueError("OCR raw page shape is invalid")
    lines: list[AdapterLine] = []
    for raw_line in page_block.lines:
        if len(raw_line.spans) != 1:
            raise ValueError("OCR raw page shape is invalid")
        span = raw_line.spans[0]
        if (
            raw_line.bbox != span.bbox
            or raw_line.confidence != span.confidence
            or span.semantic_hint is None
            or span.bbox.x0 < 0.0
            or span.bbox.y0 < 0.0
            or span.bbox.x0 >= span.bbox.x1
            or span.bbox.y0 >= span.bbox.y1
            or span.bbox.x1 > raw_page.page_width
            or span.bbox.y1 > raw_page.page_height
        ):
            raise ValueError("OCR raw page shape is invalid")
        lines.append(
            AdapterLine(
                text=span.text,
                bbox=(span.bbox.x0, span.bbox.y0, span.bbox.x1, span.bbox.y1),
                confidence=span.confidence,
                field_type=span.semantic_hint,
            )
        )
    return _review_queue(
        tuple(lines),
        source_sha256=source_sha256,
        pdf_page_index=raw_page.pdf_page_index,
    )


def _critical_review(
    edition_year: int,
    layout_evidence: LayoutEvidence,
    *,
    registry_segment_available: bool = True,
) -> tuple[
    Literal[
        "all-fields-human-verification", "stratified-sample-with-layout-escalation"
    ],
    tuple[CriticalFieldStatus, ...],
    Literal["needs_review", "machine_extracted"],
]:
    if edition_year in (2023, 2024):
        return (
            "all-fields-human-verification",
            tuple(
                CriticalFieldStatus(
                    field_type=field_type, status="unverified", review_required=True
                )
                for field_type in _CRITICAL_FIELD_TYPES
            ),
            "needs_review",
        )
    review_required = (
        layout_evidence.status != "not_detected" or not registry_segment_available
    )
    return (
        "stratified-sample-with-layout-escalation",
        tuple(
            CriticalFieldStatus(
                field_type=field_type,
                status="sampling_required",
                review_required=review_required,
            )
            for field_type in _CRITICAL_FIELD_TYPES
        ),
        "needs_review" if review_required else "machine_extracted",
    )


def _layout_segment_provenance(
    edition_year: int,
    raw_page: RawPage,
    *,
    source_sha256: str,
    segment_start_pdf_page: int,
    segment_end_pdf_page: int,
) -> LayoutSegmentProvenance | None:
    evidence = raw_page.layout_evidence
    if edition_year not in (2024, 2025):
        return None
    registry_entry = APPROVED_LAYOUT_SEGMENT_REGISTRY.get(edition_year)
    if (
        registry_entry is None
        or raw_page.doc_id != registry_entry.doc_id
        or source_sha256 != registry_entry.source_sha256
        or segment_start_pdf_page != registry_entry.segment_start_pdf_page
        or segment_end_pdf_page != registry_entry.segment_end_pdf_page
    ):
        return None
    if not (
        1 <= segment_start_pdf_page <= raw_page.pdf_page_index <= segment_end_pdf_page
    ):
        return None
    sampling_status: Literal["all_cases_required", "sampling_required"] = (
        "all_cases_required" if edition_year == 2024 else "sampling_required"
    )
    registry_payload = {
        "detector_version": APPROVED_LAYOUT_DETECTOR_VERSION,
        "doc_id": raw_page.doc_id,
        "edition_year": edition_year,
        "policy_version": LAYOUT_SEGMENT_REGISTRY_POLICY_VERSION,
        "sampling_status": sampling_status,
        "segment_end_pdf_page": segment_end_pdf_page,
        "segment_key": "approved-document-body",
        "segment_start_pdf_page": segment_start_pdf_page,
        "source_sha256": source_sha256,
    }
    registry_sha256 = hashlib.sha256(
        _LAYOUT_SEGMENT_REGISTRY_HASH_PREFIX
        + json.dumps(
            registry_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return LayoutSegmentProvenance(
        segment_id="layout-segment-"
        + hashlib.sha256(
            _LAYOUT_SEGMENT_ID_HASH_PREFIX + registry_sha256.encode("ascii")
        ).hexdigest()[:32],
        segment_key="approved-document-body",
        segment_start_pdf_page=segment_start_pdf_page,
        segment_end_pdf_page=segment_end_pdf_page,
        registry_policy_version=LAYOUT_SEGMENT_REGISTRY_POLICY_VERSION,
        registry_sha256=registry_sha256,
        detector_version=APPROVED_LAYOUT_DETECTOR_VERSION,
        region_count=len(evidence.regions),
        sampling_status=sampling_status,
    )


def _raw_ocr_page(
    *,
    document: SourceDocument,
    pdf_page_index: int,
    page_label: str | None,
    page_width: float,
    page_height: float,
    render_sha256: str,
    lines: tuple[AdapterLine, ...],
    layout_evidence: LayoutEvidence,
) -> RawPage:
    raw_lines = tuple(
        RawLine(
            bbox=BoundingBox.from_tuple(line.bbox),
            confidence=line.confidence,
            spans=(
                RawSpan(
                    text=line.text,
                    bbox=BoundingBox.from_tuple(line.bbox),
                    font="",
                    size=line.bbox[3] - line.bbox[1],
                    confidence=line.confidence,
                    semantic_hint=line.field_type,
                ),
            ),
        )
        for line in lines
    )
    page_box = BoundingBox(x0=0.0, y0=0.0, x1=page_width, y1=page_height)
    return RawPage(
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        extraction_source="ocr",
        pdf_page_index=pdf_page_index,
        page_label=page_label,
        page_width=page_width,
        page_height=page_height,
        render_sha256=render_sha256,
        raw_blocks=(RawBlock(bbox=page_box, lines=raw_lines),),
        layout_evidence=layout_evidence,
    )


def _is_approved_layout_detector(detector: LayoutDetector) -> bool:
    version = inspect.getattr_static(detector, "version", None)
    return (
        type(detector) is GreenCardBorderDetector
        and type(version) is str
        and version == APPROVED_LAYOUT_DETECTOR_VERSION
    )


def extract_pages(
    pages: tuple[Any, ...],
    *,
    document: SourceDocument,
    page_indexes: tuple[int, ...],
    adapter: OcrAdapter,
    source_sha256: str,
    image_digest: str,
    layout_detector: LayoutDetector | None = None,
) -> tuple[OcrPageRecord, ...]:
    """Render and OCR selected complete pages, quarantining expected page failures."""
    if document.extraction_method != "ocr" or document.edition_year not in (
        2023,
        2024,
        2025,
    ):
        raise ValueError("document is not approved for OCR extraction")
    policy = ocr_policy(document.edition_year)
    if document.render_dpi != policy.render_dpi:
        raise ValueError("manifest render DPI does not match OCR policy")
    if source_sha256 != document.sha256 or _SHA256_RE.fullmatch(source_sha256) is None:
        raise ValueError("source SHA-256 does not match approved document")
    if _IMAGE_DIGEST_RE.fullmatch(image_digest) is None:
        raise ValueError(
            "SEN_QA_INGESTION_IMAGE_DIGEST must be sha256:<64 lowercase hex>"
        )
    if (
        not page_indexes
        or tuple(sorted(page_indexes)) != page_indexes
        or len(set(page_indexes)) != len(page_indexes)
        or page_indexes[0] < 1
        or page_indexes[-1] > document.pdf_page_count
        or document.pdf_page_count != len(pages)
    ):
        raise ValueError("page selection is invalid")
    if layout_detector is not None and (
        document.edition_year == 2023
        or not _is_approved_layout_detector(layout_detector)
    ):
        raise OcrExtractionError("layout detector is not approved")

    records: list[OcrPageRecord] = []
    for pdf_page_index in page_indexes:
        label = printed_page_label(
            document.edition_year, pdf_page_index, policy=document.page_numbering
        )
        try:
            page = pages[pdf_page_index - 1]
            page_rect = page.rect
            rotated_width = float(page_rect.width)
            rotated_height = float(page_rect.height)
            if (
                not math.isfinite(rotated_width)
                or not math.isfinite(rotated_height)
                or rotated_width <= 0
                or rotated_height <= 0
            ):
                raise ValueError("page point geometry is invalid")
            derotation_matrix = page.derotation_matrix
            pixmap = page.get_pixmap(dpi=policy.render_dpi, alpha=False)
            if int(pixmap.n) != 3:
                raise OSError("render did not produce RGB8")
        except PAGE_EXTRACTION_ERRORS:
            records.append(
                QuarantinedOcrPageRecord(
                    schema_version=PAGE_RECORD_SCHEMA_VERSION,
                    doc_id=document.doc_id,
                    edition_year=document.edition_year,
                    pdf_page_index=pdf_page_index,
                    page_label=label,
                    source_sha256=source_sha256,
                    render_sha256=None,
                    render_dpi=policy.render_dpi,
                    image_digest=image_digest,
                    quality_flags=policy.quality_flags,
                    reason_code="page-render-failed",
                )
            )
            continue
        render_sha256 = _render_hash(pixmap, render_dpi=policy.render_dpi)
        raster_image = RasterImage(
            width=int(pixmap.width),
            height=int(pixmap.height),
            rgb_bytes=bytes(pixmap.samples),
        )
        try:
            adapter_lines = adapter.recognize(raster_image)
        except OcrAdapterError:
            records.append(
                QuarantinedOcrPageRecord(
                    schema_version=PAGE_RECORD_SCHEMA_VERSION,
                    doc_id=document.doc_id,
                    edition_year=document.edition_year,
                    pdf_page_index=pdf_page_index,
                    page_label=label,
                    source_sha256=source_sha256,
                    render_sha256=render_sha256,
                    render_dpi=policy.render_dpi,
                    image_digest=image_digest,
                    quality_flags=policy.quality_flags,
                    reason_code="ocr-adapter-failed",
                )
            )
            continue
        try:
            ordered_pixel_lines = sort_reading_order(
                adapter_lines, page_width=float(pixmap.width)
            )
            point_lines, page_width, page_height = _scale_lines_to_pdf_points(
                ordered_pixel_lines,
                page_rect=page_rect,
                derotation_matrix=derotation_matrix,
                raster_width=int(pixmap.width),
                raster_height=int(pixmap.height),
            )
            layout_evidence = _layout_evidence(
                document.edition_year,
                raster_image,
                layout_detector,
                page_rect=page_rect,
                derotation_matrix=derotation_matrix,
            )
            raw_page = _raw_ocr_page(
                document=document,
                pdf_page_index=pdf_page_index,
                page_label=label,
                page_width=page_width,
                page_height=page_height,
                render_sha256=render_sha256,
                lines=point_lines,
                layout_evidence=layout_evidence,
            )
            layout_segment = _layout_segment_provenance(
                document.edition_year,
                raw_page,
                source_sha256=source_sha256,
                segment_start_pdf_page=(document.page_numbering.body_start_pdf_page),
                segment_end_pdf_page=document.page_numbering.body_end_pdf_page,
            )
            critical_policy, critical_fields, review_status = _critical_review(
                document.edition_year,
                layout_evidence,
                registry_segment_available=layout_segment is not None,
            )
            record = ExtractedOcrPageRecord(
                schema_version=PAGE_RECORD_SCHEMA_VERSION,
                doc_id=document.doc_id,
                edition_year=document.edition_year,
                pdf_page_index=pdf_page_index,
                page_label=label,
                source_sha256=source_sha256,
                render_sha256=render_sha256,
                render_dpi=policy.render_dpi,
                image_digest=image_digest,
                quality_flags=policy.quality_flags,
                raw_page=raw_page,
                layout_segment_provenance=layout_segment,
                review_queue=_review_queue(
                    point_lines,
                    source_sha256=source_sha256,
                    pdf_page_index=pdf_page_index,
                ),
                critical_review_policy=critical_policy,
                critical_fields=critical_fields,
                review_status=review_status,
            )
        except (TypeError, ValueError):
            records.append(
                QuarantinedOcrPageRecord(
                    schema_version=PAGE_RECORD_SCHEMA_VERSION,
                    doc_id=document.doc_id,
                    edition_year=document.edition_year,
                    pdf_page_index=pdf_page_index,
                    page_label=label,
                    source_sha256=source_sha256,
                    render_sha256=render_sha256,
                    render_dpi=policy.render_dpi,
                    image_digest=image_digest,
                    quality_flags=policy.quality_flags,
                    reason_code="ocr-provenance-invalid",
                )
            )
            continue
        records.append(record)
    return tuple(records)


def write_ocr_jsonl(
    output_path: Path,
    records: tuple[OcrPageRecord, ...],
    *,
    document: SourceDocument,
    expected_image_digest: str,
    selected_page_indexes: tuple[int, ...],
) -> None:
    """Atomically write selected OCR page records in deterministic page order."""
    approved_document = revalidate_source_document(document)
    if (
        approved_document is None
        or approved_document.extraction_method != "ocr"
        or approved_document.edition_year not in _OCR_POLICIES
        or approved_document.render_dpi
        != _OCR_POLICIES[approved_document.edition_year].render_dpi
    ):
        raise OcrExtractionError("approved OCR document contract is invalid")
    if (
        type(expected_image_digest) is not str
        or _IMAGE_DIGEST_RE.fullmatch(expected_image_digest) is None
    ):
        raise OcrExtractionError("approved OCR image digest is invalid")
    if (
        type(selected_page_indexes) is not tuple
        or not selected_page_indexes
        or any(type(index) is not int for index in selected_page_indexes)
        or tuple(sorted(selected_page_indexes)) != selected_page_indexes
        or len(set(selected_page_indexes)) != len(selected_page_indexes)
        or selected_page_indexes[0] < 1
        or selected_page_indexes[-1] > approved_document.pdf_page_count
    ):
        raise OcrExtractionError("approved OCR selected pages are invalid")
    records = tuple(validate_ocr_page_record(record) for record in records)
    ordered = sorted(records, key=lambda record: record.pdf_page_index)
    indexes = tuple(record.pdf_page_index for record in ordered)
    if indexes != selected_page_indexes:
        raise OcrExtractionError("OCR page records do not match selected pages")
    policy = _OCR_POLICIES[approved_document.edition_year]
    if any(
        record.doc_id != approved_document.doc_id
        or record.edition_year != approved_document.edition_year
        or record.source_sha256 != approved_document.sha256
        or record.render_dpi != policy.render_dpi
        or record.image_digest != expected_image_digest
        or record.quality_flags != policy.quality_flags
        or record.page_label
        != printed_page_label(
            approved_document.edition_year,
            record.pdf_page_index,
            policy=approved_document.page_numbering,
        )
        for record in ordered
    ):
        raise OcrExtractionError("OCR page records do not match approved run")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = "".join(
        json.dumps(
            record.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        for record in ordered
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, output_path)
    except OSError:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise OcrExtractionError("cannot write OCR extraction output") from None


def extract_ocr_document(
    source_path: Path,
    document: SourceDocument,
    page_indexes: tuple[int, ...],
    adapter: OcrAdapter,
    image_digest: str,
) -> tuple[OcrPageRecord, ...]:
    """Open one verified PDF and extract selected pages while it remains live."""
    approved_document = revalidate_source_document(document)
    if approved_document is None:
        raise OcrExtractionError("approved document contract is invalid") from None
    document = approved_document
    if (
        document.extraction_method != "ocr"
        or document.edition_year not in _OCR_POLICIES
        or document.render_dpi != _OCR_POLICIES[document.edition_year].render_dpi
    ):
        raise OcrExtractionError("approved OCR document contract is invalid") from None
    if (
        type(image_digest) is not str
        or _IMAGE_DIGEST_RE.fullmatch(image_digest) is None
    ):
        raise OcrExtractionError("approved OCR image digest is invalid") from None
    if (
        type(page_indexes) is not tuple
        or not page_indexes
        or any(type(index) is not int for index in page_indexes)
        or tuple(sorted(page_indexes)) != page_indexes
        or len(set(page_indexes)) != len(page_indexes)
        or page_indexes[0] < 1
        or page_indexes[-1] > document.pdf_page_count
    ):
        raise OcrExtractionError("approved OCR selected pages are invalid") from None
    try:
        import pymupdf

        pdf: Any = pymupdf.open(source_path)  # type: ignore[no-untyped-call]
    except DOCUMENT_IO_ERRORS:
        raise OcrExtractionError("cannot open approved OCR source PDF") from None
    try:
        if len(pdf) != document.pdf_page_count:
            raise OcrExtractionError("approved OCR source page count changed")
        pages = tuple(pdf[index] for index in range(document.pdf_page_count))
        return extract_pages(
            pages,
            document=document,
            page_indexes=page_indexes,
            adapter=adapter,
            source_sha256=document.sha256,
            image_digest=image_digest,
            layout_detector=(
                GreenCardBorderDetector()
                if document.edition_year in (2024, 2025)
                else None
            ),
        )
    finally:
        try:
            pdf.close()
        except DOCUMENT_IO_ERRORS:
            raise OcrExtractionError("cannot close approved OCR source PDF") from None
