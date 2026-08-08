"""Offline OCR extraction for the approved 2023-2025 PDFs."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple, Protocol, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.ingestion.extract_common import (
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
    printed_page_label,
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
FieldType = Literal[
    "title",
    "question",
    "amount",
    "date",
    "law_name",
    "article",
    "document_number",
    "ocr_line",
]
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


class RasterImage(NamedTuple):
    """Dependency-free RGB raster passed to an injected OCR adapter."""

    width: int
    height: int
    rgb_bytes: bytes


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
            array = self._numpy.frombuffer(
                image.rgb_bytes, dtype=self._numpy.uint8
            ).reshape((image.height, image.width, 3))[:, :, ::-1].copy()
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

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    location_id: str = Field(pattern=r"^loc-[0-9a-f]{32}$")
    field_type: FieldType
    confidence: float = Field(ge=0.0, le=1.0)
    reason: Literal["low-confidence", "invalid-document-number"]


class CriticalFieldStatus(BaseModel):
    """A parser-blocking critical-field review marker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field_type: CriticalFieldType
    status: Literal["unverified", "sampling_required"]
    review_required: bool


class ExtractedOcrPageRecord(BaseModel):
    """Successful OCR page with immutable source, raster, and review provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["extracted"] = "extracted"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    source_sha256: str
    render_sha256: str
    render_dpi: int
    image_digest: str
    quality_flags: tuple[str, ...]
    raw_page: RawPage
    review_queue: tuple[ReviewEntry, ...]
    critical_review_policy: Literal[
        "all-fields-human-verification", "stratified-sample-with-layout-escalation"
    ]
    critical_fields: tuple[CriticalFieldStatus, ...]
    review_status: Literal["needs_review", "machine_extracted"]
    search_eligible: Literal[False] = False
    answer_eligible: Literal[False] = False


class QuarantinedOcrPageRecord(BaseModel):
    """Sanitized failed-page record; recognized text and exception detail are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["quarantined"] = "quarantined"
    doc_id: str
    edition_year: int
    pdf_page_index: int = Field(ge=1)
    page_label: str | None
    source_sha256: str
    render_sha256: str | None
    render_dpi: int
    image_digest: str
    quality_flags: tuple[str, ...]
    reason_code: Literal["page-render-failed", "ocr-adapter-failed"]


OcrPageRecord: TypeAlias = ExtractedOcrPageRecord | QuarantinedOcrPageRecord


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

    normalized_payload = dict(payload)
    normalized_payload["models"] = tuple(
        {**model_payload, "files": tuple(model_payload["files"])}
        for model_payload in models_payload
    )
    try:
        return ModelLock.model_validate(normalized_payload)
    except ValidationError as error:
        raise ModelLockError("invalid model lock structure") from error


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
        derotate(x, y)
        for x in (rect_x0, rect_x1)
        for y in (rect_y0, rect_y1)
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
        rotated_corners = (
            (rect_x0 + x0 * x_scale, rect_y0 + y0 * y_scale),
            (rect_x0 + x0 * x_scale, rect_y0 + y1 * y_scale),
            (rect_x0 + x1 * x_scale, rect_y0 + y0 * y_scale),
            (rect_x0 + x1 * x_scale, rect_y0 + y1 * y_scale),
        )
        page_points = tuple(derotate(x, y) for x, y in rotated_corners)
        point_lines.append(
            AdapterLine(
                text=line.text,
                bbox=(
                    min(point[0] for point in page_points) - page_x0,
                    min(point[1] for point in page_points) - page_y0,
                    max(point[0] for point in page_points) - page_x0,
                    max(point[1] for point in page_points) - page_y0,
                ),
                confidence=line.confidence,
                field_type=line.field_type,
            )
        )
    return tuple(point_lines), page_width, page_height


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


def _critical_review(
    edition_year: int,
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
    return (
        "stratified-sample-with-layout-escalation",
        tuple(
            CriticalFieldStatus(
                field_type=field_type,
                status="sampling_required",
                review_required=False,
            )
            for field_type in _CRITICAL_FIELD_TYPES
        ),
        "machine_extracted",
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
                ),
            ),
        )
        for line in lines
    )
    page_box = BoundingBox(
        x0=0.0, y0=0.0, x1=page_width, y1=page_height
    )
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
    )


def extract_pages(
    pages: tuple[Any, ...],
    *,
    document: SourceDocument,
    page_indexes: tuple[int, ...],
    adapter: OcrAdapter,
    source_sha256: str,
    image_digest: str,
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
            pixmap = page.get_pixmap(
                dpi=policy.render_dpi, alpha=False
            )
            if int(pixmap.n) != 3:
                raise OSError("render did not produce RGB8")
        except PAGE_EXTRACTION_ERRORS:
            records.append(
                QuarantinedOcrPageRecord(
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
        try:
            adapter_lines = adapter.recognize(
                RasterImage(
                    width=int(pixmap.width),
                    height=int(pixmap.height),
                    rgb_bytes=bytes(pixmap.samples),
                )
            )
        except OcrAdapterError:
            records.append(
                QuarantinedOcrPageRecord(
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
        raw_page = _raw_ocr_page(
            document=document,
            pdf_page_index=pdf_page_index,
            page_label=label,
            page_width=page_width,
            page_height=page_height,
            render_sha256=render_sha256,
            lines=point_lines,
        )
        critical_policy, critical_fields, review_status = _critical_review(
            document.edition_year
        )
        records.append(
            ExtractedOcrPageRecord(
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
                review_queue=_review_queue(
                    point_lines,
                    source_sha256=source_sha256,
                    pdf_page_index=pdf_page_index,
                ),
                critical_review_policy=critical_policy,
                critical_fields=critical_fields,
                review_status=review_status,
            )
        )
    return tuple(records)


def write_ocr_jsonl(output_path: Path, records: tuple[OcrPageRecord, ...]) -> None:
    """Atomically write selected OCR page records in deterministic page order."""
    ordered = sorted(records, key=lambda record: record.pdf_page_index)
    indexes = [record.pdf_page_index for record in ordered]
    if not indexes or indexes != sorted(set(indexes)):
        raise ValueError("OCR page records must have unique page indexes")
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
    except OSError as error:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise ValueError("cannot write OCR extraction output") from error


def extract_ocr_document(
    source_path: Path,
    document: SourceDocument,
    page_indexes: tuple[int, ...],
    adapter: OcrAdapter,
    image_digest: str,
) -> tuple[OcrPageRecord, ...]:
    """Open one verified PDF and extract selected pages while it remains live."""
    try:
        import pymupdf

        pdf: Any = pymupdf.open(source_path)  # type: ignore[no-untyped-call]
    except DOCUMENT_IO_ERRORS as error:
        raise OcrExtractionError("cannot open approved OCR source PDF") from error
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
        )
    finally:
        try:
            pdf.close()
        except DOCUMENT_IO_ERRORS as error:
            raise OcrExtractionError("cannot close approved OCR source PDF") from error
