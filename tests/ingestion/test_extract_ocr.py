"""Behavior contracts for offline, provenance-preserving OCR extraction."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import subprocess
import sys
import tarfile
import tomllib
import traceback
import warnings
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

import src.cli as cli_module
import src.ingestion.extract_common as common_models
import src.ingestion.extract_ocr as ocr_module
from docker.prepare_ocr_models import prepare_model_staging
from src.cli import _parse_ocr_pages, app
from src.ingestion.extract_common import (
    APPROVED_LAYOUT_DETECTOR_VERSION,
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
)
from src.ingestion.extract_ocr import (
    APPROVED_LAYOUT_SEGMENT_REGISTRY,
    AdapterLine,
    ExtractedOcrPageRecord,
    GreenCardBorderDetector,
    ModelLockError,
    OcrAdapterError,
    OcrExtractionError,
    PaddleOcrAdapter,
    QuarantinedOcrPageRecord,
    RasterImage,
    extract_pages,
    load_model_lock,
    ocr_policy,
    sort_reading_order,
    validate_installed_models,
    validate_model_lock,
    validate_ocr_page_record,
    write_ocr_jsonl,
)
from src.ingestion.manifest import SourceDocument, load_manifest


def _valid_model_lock_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "language": "korean",
        "packages": {"paddleocr": "3.7.0", "paddlepaddle": "3.1.1"},
        "models": [
            {
                "name": "detector",
                "revision": "paddle3.0.0",
                "source_url": "https://paddle-model-ecology.bj.bcebos.com/models/paddle3.0.0/detector.tar",
                "archive_sha256": "a" * 64,
                "files": [
                    {
                        "path": "inference.pdiparams",
                        "sha256": "99c09b3f09a17b1f626545eb1b8f732bd3bf621a603b6a948fb271dba7784585",
                    }
                ],
            },
            {
                "name": "recognizer",
                "revision": "paddle3.0.0",
                "source_url": "https://paddle-model-ecology.bj.bcebos.com/models/paddle3.0.0/recognizer.tar",
                "archive_sha256": "b" * 64,
                "files": [
                    {
                        "path": "inference.pdiparams",
                        "sha256": "3958092ac074021bc1fb33aa9a01ca1e182d1712b0ffd5212f069402105623ef",
                    }
                ],
            },
        ],
    }


class FakePixmap:
    def __init__(
        self,
        *,
        width: int = 1000,
        height: int = 1400,
        samples: bytes = bytes(range(12)),
    ) -> None:
        self.width = width
        self.height = height
        self.n = 3
        self.samples = samples


class FakePage:
    def __init__(
        self,
        *,
        failure: Exception | None = None,
        point_width: float = 1000.0,
        point_height: float = 1400.0,
        pixmap: FakePixmap | None = None,
    ) -> None:
        self.failure = failure
        self.rect = SimpleNamespace(
            x0=0.0,
            y0=0.0,
            x1=point_width,
            y1=point_height,
            width=point_width,
            height=point_height,
        )
        self.derotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        self.pixmap = pixmap or FakePixmap()
        self.calls: list[dict[str, object]] = []

    def get_pixmap(self, **kwargs: object) -> FakePixmap:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return self.pixmap


class FakeAdapter:
    def __init__(
        self,
        lines: tuple[AdapterLine, ...],
        *,
        failing_calls: frozenset[int] = frozenset(),
        expected_width: int = 1000,
        expected_height: int = 1400,
    ) -> None:
        self.lines = lines
        self.failing_calls = failing_calls
        self.expected_width = expected_width
        self.expected_height = expected_height
        self.calls = 0

    def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
        assert image.width == self.expected_width
        assert image.height == self.expected_height
        assert image.rgb_bytes == bytes(range(12))
        self.calls += 1
        if self.calls in self.failing_calls:
            raise OcrAdapterError("PRIVATE OCR ENGINE DETAIL")
        return self.lines


def _ocr_document(*, year: int = 2025, pages: int = 1) -> SourceDocument:
    source_dpi = {2023: 96, 2024: 150, 2025: 300}[year]
    render_dpi = {2023: 300, 2024: 350, 2025: 300}[year]
    return SourceDocument.model_validate(
        {
            "doc_id": f"fixture-{year}",
            "edition_year": year,
            "official_title": "Synthetic fixture",
            "publisher": "Fixture",
            "registration_no": None,
            "source_period_start": None,
            "source_period_end": None,
            "source_filename": f"{year}.pdf",
            "source_relpath": f"{year}.pdf",
            "sha256": "c" * 64,
            "pdf_page_count": pages,
            "page_size_profiles": (
                {
                    "start_pdf_page": 1,
                    "end_pdf_page": pages,
                    "width_pt": 595.0,
                    "height_pt": 841.0,
                },
            ),
            "extraction_method": "ocr",
            "source_dpi": source_dpi,
            "render_dpi": render_dpi,
            "page_numbering": {
                "mode": "offset",
                "body_start_pdf_page": 1,
                "body_end_pdf_page": pages,
                "offset": 0,
            },
            "official_public_url": None,
            "official_url_status": "unverified",
            "redistribution_status": "unverified",
            "access_level": "staff",
        }
    )


def _approved_segment_document(year: int) -> SourceDocument:
    entry = APPROVED_LAYOUT_SEGMENT_REGISTRY[year]
    page_count = entry.segment_end_pdf_page + 1
    payload = _ocr_document(year=year, pages=page_count).model_dump()
    payload["doc_id"] = entry.doc_id
    payload["sha256"] = entry.source_sha256
    payload["page_numbering"] = {
        "mode": "offset",
        "body_start_pdf_page": entry.segment_start_pdf_page,
        "body_end_pdf_page": entry.segment_end_pdf_page,
        "offset": 0,
    }
    return SourceDocument.model_validate(payload)


def _fixture_lines(name: str) -> tuple[AdapterLine, ...]:
    payload = json.loads(
        (Path("tests/fixtures/ocr-pages") / name).read_text(encoding="utf-8")
    )
    return tuple(
        AdapterLine.model_validate({**line, "bbox": tuple(line["bbox"])})
        for line in payload["lines"]
    )


def _extracted_record(record: object) -> ExtractedOcrPageRecord:
    assert isinstance(record, ExtractedOcrPageRecord)
    return record


def _quarantined_record(record: object) -> QuarantinedOcrPageRecord:
    assert isinstance(record, QuarantinedOcrPageRecord)
    return record


def _trusted_layout_detector(
    monkeypatch: pytest.MonkeyPatch, delegate: ocr_module.LayoutDetector
) -> GreenCardBorderDetector:
    """Route test behavior through the exact sealed production detector type."""

    def detect(
        self: GreenCardBorderDetector, image: RasterImage
    ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
        return delegate.detect(image)

    monkeypatch.setattr(GreenCardBorderDetector, "detect", detect)
    return GreenCardBorderDetector()


def _tar_archive(
    model_name: str, files: dict[str, bytes], *, unsafe_name: str | None = None
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for relative_path, content in files.items():
            info = tarfile.TarInfo(name=f"{model_name}/{relative_path}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if unsafe_name is not None:
            content = b"escape"
            info = tarfile.TarInfo(name=unsafe_name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _patch_successful_ocr_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Path,
    document: SourceDocument,
    records: tuple[object, ...],
) -> None:
    monkeypatch.setattr(cli_module, "load_manifest", lambda path: (document,))
    monkeypatch.setattr(cli_module, "resolve_source", lambda root, selected: source)
    monkeypatch.setattr(cli_module, "verify_source", lambda path, selected: None)
    monkeypatch.setattr(
        cli_module, "validate_installed_models", lambda lock, root: None
    )
    monkeypatch.setattr(
        cli_module, "create_paddle_adapter", lambda lock, root: FakeAdapter(())
    )
    monkeypatch.setattr(
        cli_module,
        "extract_ocr_document",
        lambda path, selected, page_indexes, adapter, image_digest: records,
    )


def test_ocr_render_policies_are_fixed() -> None:
    """Catches rendering a reviewed annual quality group at an unapproved DPI."""
    assert ocr_policy(2023).render_dpi == 300
    assert ocr_policy(2023).quality_flags == ("source_approx_96dpi",)
    assert ocr_policy(2024).render_dpi == 350
    assert ocr_policy(2024).quality_flags == ("source_150dpi",)
    assert ocr_policy(2025).render_dpi == 300
    assert ocr_policy(2025).quality_flags == ()


def test_layout_segment_registry_matches_approved_source_manifest() -> None:
    documents = load_manifest(Path("data/manifests/sen_qa_sources.json"))
    actual = {
        document.edition_year: (
            document.doc_id,
            document.sha256,
            document.page_numbering.body_start_pdf_page,
            document.page_numbering.body_end_pdf_page,
        )
        for document in documents
        if document.edition_year in APPROVED_LAYOUT_SEGMENT_REGISTRY
    }
    expected = {
        year: (
            entry.doc_id,
            entry.source_sha256,
            entry.segment_start_pdf_page,
            entry.segment_end_pdf_page,
        )
        for year, entry in APPROVED_LAYOUT_SEGMENT_REGISTRY.items()
    }
    assert actual == expected


def test_checked_in_model_lock_matches_frozen_runtime_packages() -> None:
    """Catches model metadata drifting from the exact uv.lock/runtime packages."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    lock = load_model_lock(Path("config/models.lock.json"))
    paddle_package = next(
        package for package in uv_lock["package"] if package["name"] == "paddlepaddle"
    )
    cp311_linux_wheel = next(
        wheel
        for wheel in paddle_package["wheels"]
        if "cp311-cp311-manylinux1_x86_64.whl" in wheel["url"]
    )

    assert (
        "paddlepaddle==3.1.1; sys_platform == 'linux' and "
        "platform_machine == 'x86_64'"
        in project["project"]["optional-dependencies"]["ocr"]
    )
    assert paddle_package["version"] == "3.1.1"
    assert {key: cp311_linux_wheel[key] for key in ("url", "hash", "size")} == {
        "url": "https://files.pythonhosted.org/packages/78/e5/8c8a2a73a745d38433711ef8c54bc4326fe0e763ed4468e87f7e9e0fb837/paddlepaddle-3.1.1-cp311-cp311-manylinux1_x86_64.whl",
        "hash": "sha256:36c6a768d31486c100e1be14404f8fc57565283f0df90b7142d2560100fe86ef",
        "size": 187453011,
    }
    assert lock.language == "korean"
    assert lock.packages.paddleocr == "3.7.0"
    assert lock.packages.paddlepaddle == "3.1.1"
    assert {model.name for model in lock.models} == {
        "PP-OCRv5_server_det_infer",
        "korean_PP-OCRv5_mobile_rec_infer",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(schema_version=2), "schema version"),
        (lambda payload: payload.update(language="english"), "language"),
        (
            lambda payload: payload["packages"].update(paddleocr="3.6.0"),
            "package versions",
        ),
        (lambda payload: payload["models"][0].update(revision=""), "revision"),
        (lambda payload: payload["models"][0].update(revision="latest"), "revision"),
        (
            lambda payload: payload["models"][0].update(
                source_url="https://example.invalid/model.tar"
            ),
            "source",
        ),
        (
            lambda payload: payload["models"][0].update(archive_sha256="A" * 64),
            "SHA-256",
        ),
        (
            lambda payload: payload["models"][0]["files"][0].update(path="../escape"),
            "path",
        ),
        (
            lambda payload: payload["models"][0]["files"].append(
                deepcopy(payload["models"][0]["files"][0])
            ),
            "duplicate",
        ),
    ],
)
def test_model_lock_rejects_untrusted_or_mutable_metadata(
    mutation: object, message: str
) -> None:
    """Catches mutable sources, unsafe paths, weak hashes, and runtime drift."""
    payload = _valid_model_lock_payload()
    mutation(payload)  # type: ignore[operator]

    with pytest.raises(ModelLockError, match=message):
        validate_model_lock(payload)


def test_model_directory_validation_detects_missing_and_tampered_files(
    tmp_path: Path,
) -> None:
    """Catches runtime accepting an incomplete or modified local model cache."""
    lock = validate_model_lock(_valid_model_lock_payload())
    (tmp_path / "detector").mkdir()
    (tmp_path / "recognizer").mkdir()
    (tmp_path / "detector/inference.pdiparams").write_bytes(b"det-model")

    with pytest.raises(ModelLockError, match="missing locked model file"):
        validate_installed_models(lock, tmp_path)

    (tmp_path / "recognizer/inference.pdiparams").write_bytes(b"tampered")
    with pytest.raises(ModelLockError, match="model file SHA-256 mismatch"):
        validate_installed_models(lock, tmp_path)

    (tmp_path / "recognizer/inference.pdiparams").write_bytes(b"rec-model")
    validate_installed_models(lock, tmp_path)


def test_model_lock_loader_never_echoes_malformed_private_content(
    tmp_path: Path,
) -> None:
    """Catches malformed lock bytes leaking through a diagnostic message."""
    lock_path = tmp_path / "models.lock.json"
    lock_path.write_text('{"private":"DO-NOT-ECHO"', encoding="utf-8")

    with pytest.raises(ModelLockError, match="cannot parse model lock") as captured:
        load_model_lock(lock_path)

    assert "DO-NOT-ECHO" not in str(captured.value)


def test_host_import_does_not_import_or_require_paddle() -> None:
    """Catches a module-level Paddle import breaking dependency-free host tests."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import src.ingestion.extract_ocr; "
                "assert 'paddle' not in sys.modules and 'paddleocr' not in sys.modules"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""


def test_paddle_adapter_selects_locked_model_names_and_preserves_result_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches Paddle selecting default model families despite locked model directories."""
    captured: dict[str, object] = {}
    predicted_images: list[object] = []

    class FakeRasterArray:
        def __init__(self, values: list[int]) -> None:
            self.values = values

        def reshape(self, shape: tuple[int, int, int]) -> FakeRasterArray:
            assert shape == (1, 1, 3)
            return self

        def __getitem__(self, key: object) -> FakeRasterArray:
            assert key == (slice(None), slice(None), slice(None, None, -1))
            return FakeRasterArray(list(reversed(self.values)))

        def copy(self) -> FakeRasterArray:
            return FakeRasterArray(self.values.copy())

        def tolist(self) -> list[list[list[int]]]:
            return [[self.values]]

    class FakeVector:
        def __init__(self, values: list[float]) -> None:
            self.values = values

        def min(self) -> float:
            return min(self.values)

        def max(self) -> float:
            return max(self.values)

    class FakePoints:
        def __init__(self, values: object) -> None:
            self.values = values
            self.ndim = 2
            self.shape = (4, 2)

        def __getitem__(self, key: object) -> FakeVector:
            rows = self.values
            assert isinstance(rows, list)
            assert isinstance(key, tuple) and key[0] == slice(None)
            column = key[1]
            assert isinstance(column, int)
            return FakeVector([float(row[column]) for row in rows])

    fake_numpy = SimpleNamespace(
        uint8=object(),
        frombuffer=lambda values, dtype: FakeRasterArray(list(values)),
        asarray=lambda values, dtype: FakePoints(values),
    )

    class CapturingPaddleOcr:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def predict(self, image: object) -> tuple[object, ...]:
            predicted_images.append(image.copy())  # type: ignore[attr-defined]
            return (
                SimpleNamespace(
                    json={
                        "res": {
                            "rec_texts": ["서울교육 2025-109"],
                            "rec_scores": [0.97],
                            "rec_polys": [[[1, 2], [5, 2], [5, 7], [1, 7]]],
                        }
                    }
                ),
            )

    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda name: (
            fake_numpy
            if name == "numpy"
            else SimpleNamespace(PaddleOCR=CapturingPaddleOcr)
        ),
    )
    detection_model = tmp_path / "PP-OCRv5_server_det_infer"
    recognition_model = tmp_path / "korean_PP-OCRv5_mobile_rec_infer"

    adapter = PaddleOcrAdapter(
        detection_model=detection_model, recognition_model=recognition_model
    )
    lines = adapter.recognize(RasterImage(width=1, height=1, rgb_bytes=b"\x01\x02\x03"))

    assert captured == {
        "lang": "korean",
        "text_detection_model_name": "PP-OCRv5_server_det",
        "text_detection_model_dir": str(detection_model),
        "text_recognition_model_name": "korean_PP-OCRv5_mobile_rec",
        "text_recognition_model_dir": str(recognition_model),
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "device": "cpu",
    }
    assert lines == (
        AdapterLine(
            text="서울교육 2025-109",
            bbox=(1.0, 2.0, 5.0, 7.0),
            confidence=0.97,
            field_type="document_number",
        ),
    )
    predicted_image = predicted_images[0]
    assert isinstance(predicted_image, FakeRasterArray)
    assert predicted_image.tolist() == [[[3, 2, 1]]]


def test_full_page_render_uses_reviewed_dpi_and_records_provenance() -> None:
    """Catches strip-image OCR, alpha raster drift, or missing source/render provenance."""
    page = FakePage()
    adapter = FakeAdapter(
        (
            AdapterLine(
                text="교육행정 ABC-2025",
                bbox=(10.0, 20.0, 300.0, 60.0),
                confidence=0.94,
                field_type="title",
            ),
        )
    )

    record = extract_pages(
        (page,),
        document=_ocr_document(),
        page_indexes=(1,),
        adapter=adapter,
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]

    assert page.calls == [{"dpi": 300, "alpha": False}]
    assert record.status == "extracted"
    assert record.source_sha256 == "c" * 64
    assert record.image_digest == "sha256:" + "d" * 64
    assert record.render_dpi == 300
    assert len(record.render_sha256) == 64
    assert record.raw_page.extraction_source == "ocr"
    assert record.raw_page.pdf_page_index == 1
    assert record.raw_page.page_label == "1"
    line = record.raw_page.raw_blocks[0].lines[0]
    assert line.bbox.model_dump() == {"x0": 10.0, "y0": 20.0, "x1": 300.0, "y1": 60.0}
    assert line.confidence == 0.94
    assert line.spans[0].confidence == 0.94
    assert line.spans[0].semantic_hint == "title"


def test_ocr_geometry_is_persisted_in_pdf_points_on_non_square_page() -> None:
    """Catches rendered-pixel coordinates leaking into the common PDF-point contract."""
    page = FakePage(
        point_width=200.0,
        point_height=100.0,
        pixmap=FakePixmap(width=1000, height=400),
    )
    adapter = FakeAdapter(
        (
            AdapterLine(
                text="scaled",
                bbox=(100.0, 40.0, 500.0, 200.0),
                confidence=0.94,
            ),
        ),
        expected_width=1000,
        expected_height=400,
    )

    record = extract_pages(
        (page,),
        document=_ocr_document(),
        page_indexes=(1,),
        adapter=adapter,
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record = _extracted_record(record)

    assert record.raw_page.page_width == 200.0
    assert record.raw_page.page_height == 100.0
    assert record.raw_page.raw_blocks[0].bbox.model_dump() == {
        "x0": 0.0,
        "y0": 0.0,
        "x1": 200.0,
        "y1": 100.0,
    }
    assert record.raw_page.raw_blocks[0].lines[0].bbox.model_dump() == {
        "x0": 20.0,
        "y0": 10.0,
        "x1": 100.0,
        "y1": 50.0,
    }


def test_ocr_line_outside_raster_is_quarantined_before_point_scaling() -> None:
    """Catches impossible OCR coordinates becoming parser-trusted page provenance."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2023),
        page_indexes=(1,),
        adapter=FakeAdapter(
            (
                AdapterLine(
                    text="synthetic question",
                    bbox=(10.0, 20.0, 1001.0, 60.0),
                    confidence=0.9,
                    field_type="question",
                ),
            )
        ),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]

    assert record.status == "quarantined"
    assert _quarantined_record(record).reason_code == "ocr-provenance-invalid"


def test_ocr_record_revalidation_rejects_line_outside_pdf_page() -> None:
    """Catches an impossible nested RawPage bbox bypassing the public boundary."""
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=FakeAdapter(
                (
                    AdapterLine(
                        text="synthetic question",
                        bbox=(10.0, 20.0, 300.0, 60.0),
                        confidence=0.95,
                        field_type="question",
                    ),
                )
            ),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    raw_block = record.raw_page.raw_blocks[0]
    raw_line = raw_block.lines[0]
    raw_span = raw_line.spans[0]
    outside = BoundingBox(
        x0=raw_span.bbox.x0,
        y0=raw_span.bbox.y0,
        x1=record.raw_page.page_width + 1.0,
        y1=raw_span.bbox.y1,
    )
    forged_span = RawSpan.model_validate({**raw_span.model_dump(), "bbox": outside})
    forged_line = RawLine.model_validate(
        {**raw_line.model_dump(), "bbox": outside, "spans": (forged_span,)}
    )
    forged_block = RawBlock.model_validate(
        {**raw_block.model_dump(), "lines": (forged_line,)}
    )
    forged_page = RawPage.model_validate(
        {**record.raw_page.model_dump(), "raw_blocks": (forged_block,)}
    )
    forged_record = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, "raw_page": forged_page}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged_record)


def test_equivalent_300_and_350_dpi_boxes_share_point_geometry_and_location() -> None:
    """Catches DPI-dependent persisted bboxes or opaque review locations."""
    page_300 = FakePage(
        point_width=200.0,
        point_height=100.0,
        pixmap=FakePixmap(width=600, height=300),
    )
    page_350 = FakePage(
        point_width=200.0,
        point_height=100.0,
        pixmap=FakePixmap(width=700, height=350),
    )
    line_300 = AdapterLine(
        text="low confidence",
        bbox=(60.0, 30.0, 300.0, 150.0),
        confidence=0.5,
        field_type="question",
    )
    line_350 = AdapterLine(
        text="low confidence",
        bbox=(70.0, 35.0, 350.0, 175.0),
        confidence=0.5,
        field_type="question",
    )

    record_300 = extract_pages(
        (page_300,),
        document=_ocr_document(year=2023),
        page_indexes=(1,),
        adapter=FakeAdapter((line_300,), expected_width=600, expected_height=300),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record_350 = extract_pages(
        (page_350,),
        document=_ocr_document(year=2024),
        page_indexes=(1,),
        adapter=FakeAdapter((line_350,), expected_width=700, expected_height=350),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record_300 = _extracted_record(record_300)
    record_350 = _extracted_record(record_350)

    bbox_300 = record_300.raw_page.raw_blocks[0].lines[0].bbox
    bbox_350 = record_350.raw_page.raw_blocks[0].lines[0].bbox
    assert bbox_300 == bbox_350 == BoundingBox(x0=20.0, y0=10.0, x1=100.0, y1=50.0)
    assert (
        record_300.review_queue[0].location_id == record_350.review_queue[0].location_id
    )


@pytest.mark.parametrize(
    ("rotation", "expected_bbox"),
    [
        (90, {"x0": 40.0, "y0": 40.0, "x1": 160.0, "y1": 90.0}),
        (270, {"x0": 40.0, "y0": 10.0, "x1": 160.0, "y1": 60.0}),
    ],
)
def test_rotated_ocr_boxes_are_derotated_to_bounded_unrotated_pdf_points(
    rotation: int, expected_bbox: dict[str, float]
) -> None:
    """Catches rotated raster coordinates escaping unrotated point-space page bounds."""

    class FractionalBoxAdapter:
        def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
            return (
                AdapterLine(
                    text="rotated",
                    bbox=(
                        image.width * 0.1,
                        image.height * 0.2,
                        image.width * 0.6,
                        image.height * 0.8,
                    ),
                    confidence=0.9,
                ),
            )

    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    page = pdf.new_page(width=200.0, height=100.0)
    page.set_rotation(rotation)  # type: ignore[no-untyped-call]
    try:
        record = extract_pages(
            (page,),
            document=_ocr_document(),
            page_indexes=(1,),
            adapter=FractionalBoxAdapter(),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    finally:
        pdf.close()  # type: ignore[no-untyped-call]
    record = _extracted_record(record)

    assert record.raw_page.page_width == 200.0
    assert record.raw_page.page_height == 100.0
    bbox = record.raw_page.raw_blocks[0].lines[0].bbox
    assert bbox.model_dump() == pytest.approx(expected_bbox)
    assert 0.0 <= bbox.x0 <= bbox.x1 <= record.raw_page.page_width
    assert 0.0 <= bbox.y0 <= bbox.y1 <= record.raw_page.page_height


def test_reading_order_groups_row_centers_then_applies_left_column_first() -> None:
    """Catches row jitter or right-column text interleaving into the left column."""
    lines = _fixture_lines("2025-mixed-script.json")

    ordered = sort_reading_order(lines, page_width=1000.0)

    assert tuple(line.text for line in ordered) == ("L1", "L2 혼합 ABC-12", "R1", "R2")


def test_midpoint_boundary_is_deterministically_assigned_to_right_column() -> None:
    """Catches a midpoint line changing columns due to unstable boundary comparison."""
    lines = (
        AdapterLine(text="boundary", bbox=(500.0, 10.0, 700.0, 30.0), confidence=0.9),
        AdapterLine(text="left", bbox=(10.0, 20.0, 200.0, 40.0), confidence=0.9),
    )

    assert tuple(
        line.text for line in sort_reading_order(lines, page_width=1000.0)
    ) == (
        "left",
        "boundary",
    )


def test_low_confidence_and_invalid_document_number_reviews_never_contain_values() -> (
    None
):
    """Catches sensitive recognized text leaking into the human-review queue."""
    adapter = FakeAdapter(_fixture_lines("2023-low-dpi.json"))

    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2023),
        page_indexes=(1,),
        adapter=adapter,
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record = _extracted_record(record)
    review_payload = str(tuple(entry.model_dump() for entry in record.review_queue))

    assert len(record.review_queue) == 2
    assert all(entry.location_id.startswith("loc-") for entry in record.review_queue)
    assert {entry.field_type for entry in record.review_queue} == {
        "question",
        "document_number",
    }
    assert {entry.reason for entry in record.review_queue} == {
        "low-confidence",
        "invalid-document-number",
    }
    assert "PRIVATE-LOW-VALUE" not in review_payload
    assert "PRIVATE-DOC-VALUE" not in review_payload


def test_valid_labeled_document_number_does_not_create_false_review() -> None:
    """Catches validating the display label punctuation as part of the document number value."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2023),
        page_indexes=(1,),
        adapter=FakeAdapter(
            (
                AdapterLine(
                    text="문서번호: 서울교육 2025-109",
                    bbox=(10.0, 150.0, 500.0, 180.0),
                    confidence=0.96,
                    field_type="document_number",
                ),
            )
        ),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record = _extracted_record(record)

    assert record.review_queue == ()


@pytest.mark.parametrize("year", [2023, 2024])
def test_low_resolution_pages_block_all_critical_fields_until_human_review(
    year: int,
) -> None:
    """Catches a later parser approving unverified low-resolution critical fields."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=year),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record = _extracted_record(record)

    assert record.critical_review_policy == "all-fields-human-verification"
    assert {field.field_type for field in record.critical_fields} == {
        "title",
        "question",
        "amount",
        "date",
        "law_name",
        "article",
    }
    assert all(
        field.status == "unverified" and field.review_required
        for field in record.critical_fields
    )
    assert record.review_status == "needs_review"
    assert record.search_eligible is False
    assert record.answer_eligible is False


def test_2025_records_explicit_sample_and_layout_escalation_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches 2025 machine output being treated as answer-approved by omission."""

    class EmptyApprovedLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return ()

    document = _approved_segment_document(2025)
    record = extract_pages(
        tuple(FakePage() for _ in range(document.pdf_page_count)),
        document=document,
        page_indexes=(7,),
        adapter=FakeAdapter(()),
        source_sha256=document.sha256,
        image_digest="sha256:" + "d" * 64,
        layout_detector=_trusted_layout_detector(
            monkeypatch, EmptyApprovedLayoutDetector()
        ),
    )[0]
    record = _extracted_record(record)

    assert record.critical_review_policy == "stratified-sample-with-layout-escalation"
    assert all(field.status == "sampling_required" for field in record.critical_fields)
    assert record.layout_segment_provenance is not None
    assert record.review_status == "machine_extracted"
    assert record.search_eligible is False
    assert record.answer_eligible is False


def test_adapter_failure_is_sanitized_and_later_page_continues() -> None:
    """Catches one OCR-engine failure aborting the document or leaking engine detail."""
    records = extract_pages(
        (FakePage(), FakePage()),
        document=_ocr_document(pages=2),
        page_indexes=(1, 2),
        adapter=FakeAdapter((), failing_calls=frozenset({1})),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )

    assert [record.status for record in records] == ["quarantined", "extracted"]
    quarantined = _quarantined_record(records[0])
    assert quarantined.reason_code == "ocr-adapter-failed"
    assert quarantined.render_sha256 is not None
    assert "PRIVATE" not in str(quarantined.model_dump())


def test_page_render_failure_is_sanitized_and_later_page_continues() -> None:
    """Catches a supported page render failure becoming blank OCR output."""
    records = extract_pages(
        (FakePage(failure=OSError("PRIVATE PDF DETAIL")), FakePage()),
        document=_ocr_document(pages=2),
        page_indexes=(1, 2),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )

    assert [record.status for record in records] == ["quarantined", "extracted"]
    quarantined = _quarantined_record(records[0])
    assert quarantined.reason_code == "page-render-failed"
    assert "PRIVATE" not in str(quarantined.model_dump())


def test_supported_pymupdf_render_failure_is_quarantined_and_later_page_continues() -> (
    None
):
    """Catches MuPDF's native exception family escaping the per-page quarantine boundary."""
    records = extract_pages(
        (
            FakePage(failure=pymupdf.mupdf.FzErrorGeneric("PRIVATE PDF DETAIL")),
            FakePage(),
        ),
        document=_ocr_document(pages=2),
        page_indexes=(1, 2),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )

    assert [record.status for record in records] == ["quarantined", "extracted"]
    assert _quarantined_record(records[0]).reason_code == "page-render-failed"


@pytest.mark.parametrize(
    "digest",
    ["d" * 64, "sha256:" + "D" * 64, "sha256:short"],
)
def test_extraction_rejects_noncanonical_image_digest(digest: str) -> None:
    """Catches uninspectable or mutable container identity entering page provenance."""
    with pytest.raises(ValueError, match="SEN_QA_INGESTION_IMAGE_DIGEST"):
        extract_pages(
            (FakePage(),),
            document=_ocr_document(),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest=digest,
        )


def test_page_selection_rejects_duplicates_gaps_and_out_of_range() -> None:
    """Catches ambiguous or unsafe page indexing before accessing the PDF."""
    for page_indexes in ((1, 1), (0,), (3,)):
        with pytest.raises(ValueError, match="page selection"):
            extract_pages(
                (FakePage(), FakePage()),
                document=_ocr_document(pages=2),
                page_indexes=page_indexes,
                adapter=FakeAdapter(()),
                source_sha256="c" * 64,
                image_digest="sha256:" + "d" * 64,
            )


def test_native_page_rejects_measured_ocr_confidence_but_ocr_page_accepts_it() -> None:
    """Catches weakening native confidence while evolving the shared OCR contract."""
    bbox = BoundingBox(x0=1.0, y0=2.0, x1=3.0, y1=4.0)
    span = RawSpan(text="fixture", bbox=bbox, font="", size=2.0, confidence=0.7)
    line = RawLine(bbox=bbox, spans=(span,), confidence=0.7)
    block = RawBlock(bbox=bbox, lines=(line,))
    payload = {
        "doc_id": "fixture",
        "edition_year": 2025,
        "pdf_page_index": 1,
        "page_label": "1",
        "page_width": 10.0,
        "page_height": 20.0,
        "render_sha256": "a" * 64,
        "raw_blocks": (block,),
    }

    with pytest.raises(ValueError, match="native page confidence"):
        RawPage.model_validate({**payload, "extraction_source": "native"})

    page = RawPage.model_validate({**payload, "extraction_source": "ocr"})
    assert page.raw_blocks[0].lines[0].confidence == 0.7


def test_ocr_span_preserves_semantic_hint_for_parser_role_evidence() -> None:
    """Catches OCR field classification disappearing before yearly parsing."""
    bbox = BoundingBox(x0=1.0, y0=2.0, x1=3.0, y1=4.0)

    span = RawSpan(
        text="synthetic question marker",
        bbox=bbox,
        font="",
        size=2.0,
        confidence=0.9,
        semantic_hint="question",
    )

    assert span.semantic_hint == "question"


def test_layout_evidence_rejects_regions_outside_the_raw_page() -> None:
    """Catches fabricated or unlocatable card-border evidence entering parser input."""
    bbox = BoundingBox(x0=1.0, y0=2.0, x1=3.0, y1=4.0)
    block = RawBlock(
        bbox=bbox,
        lines=(
            RawLine(
                bbox=bbox,
                spans=(RawSpan(text="fixture", bbox=bbox, font="", size=2.0),),
            ),
        ),
    )
    region = common_models.LayoutRegion(
        region_type="card",
        bbox=BoundingBox(x0=1.0, y0=2.0, x1=11.0, y1=19.0),
        evidence="raster-border",
    )

    with pytest.raises(ValidationError, match="layout region.*page"):
        RawPage(
            doc_id="fixture",
            edition_year=2025,
            extraction_source="ocr",
            pdf_page_index=1,
            page_label="1",
            page_width=10.0,
            page_height=20.0,
            render_sha256="a" * 64,
            raw_blocks=(block,),
            layout_evidence=common_models.LayoutEvidence(
                status="detected",
                detector_version="synthetic-border-v1",
                regions=(region,),
            ),
        )

    with pytest.raises(ValidationError, match="requires regions"):
        common_models.LayoutEvidence(
            status="detected",
            detector_version="synthetic-border-v1",
        )


def test_2024_2025_without_layout_detector_records_unavailable_evidence() -> None:
    """Catches a missing card detector being mislabeled as not applicable or detected."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2025),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]

    assert record.status == "extracted"
    assert record.raw_page.layout_evidence.status == "unavailable"
    assert record.raw_page.layout_evidence.regions == ()
    assert record.review_status == "needs_review"


def test_unapproved_layout_detector_is_rejected_without_running_or_leaking_version() -> (
    None
):
    """Catches a detector self-asserting an arbitrary provenance version."""
    marker = "private-unapproved-detector-v9"

    class UnapprovedLayoutDetector:
        version = marker

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            raise AssertionError("unapproved detector must never run")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OcrExtractionError, match="not approved") as error:
            extract_pages(
                (FakePage(),),
                document=_ocr_document(year=2025),
                page_indexes=(1,),
                adapter=FakeAdapter(()),
                source_sha256="c" * 64,
                image_digest="sha256:" + "d" * 64,
                layout_detector=UnapprovedLayoutDetector(),
            )

    diagnostics = "\n".join(
        (
            str(error.value),
            repr(error.value),
            "".join(traceback.format_exception(error.value)),
            *(str(item.message) for item in caught),
        )
    )
    assert marker not in diagnostics
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert caught == []


def test_arbitrary_detector_cannot_copy_the_approved_version() -> None:
    """Catches a public version string being mistaken for implementation trust."""
    calls = 0

    class CopiedVersionDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            nonlocal calls
            calls += 1
            return ()

    with pytest.raises(OcrExtractionError, match="not approved") as error:
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2025),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
            layout_detector=CopiedVersionDetector(),
        )

    assert calls == 0
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_green_card_border_detector_finds_two_synthetic_cards_deterministically() -> (
    None
):
    """Catches broad section bars or header rules becoming fabricated card regions."""
    width, height = 100, 100
    pixels = bytearray([255] * (width * height * 3))

    def green_line(
        y_start: int, y_end: int, x_start: int = 10, x_end: int = 90
    ) -> None:
        for y in range(y_start, y_end):
            for x in range(x_start, x_end):
                offset = (y * width + x) * 3
                pixels[offset : offset + 3] = bytes((145, 198, 90))

    green_line(5, 6)
    green_line(10, 11)
    green_line(12, 18)
    green_line(20, 21)
    green_line(45, 46)
    green_line(55, 56)
    green_line(80, 81)
    for y in (20, 45):
        isolated_offset = (y * width + 99) * 3
        pixels[isolated_offset : isolated_offset + 3] = bytes((145, 198, 90))
    image = RasterImage(width=width, height=height, rgb_bytes=bytes(pixels))

    detector = ocr_module.GreenCardBorderDetector()
    first = detector.detect(image)
    second = detector.detect(image)

    assert first == second
    assert [region.bbox for region in first] == [
        (10.0, 20.0, 90.0, 46.0),
        (10.0, 55.0, 90.0, 81.0),
    ]


def test_extracted_ocr_record_rejects_envelope_raw_provenance_mismatch() -> None:
    """Catches a page envelope being rebound to unrelated raw OCR evidence."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2023),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    payload = record.model_dump()
    payload["doc_id"] = "other-document"

    with pytest.raises(ValidationError, match="OCR page envelope"):
        type(record).model_validate(payload)


def test_ocr_page_record_requires_explicit_schema_v2() -> None:
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    payload = record.model_dump()
    payload.pop("schema_version")

    with pytest.raises(ValidationError, match="schema_version"):
        type(record).model_validate(payload)

    payload["schema_version"] = 1
    with pytest.raises(ValidationError, match="schema_version"):
        type(record).model_validate(payload)


def test_ocr_document_wrapper_collects_real_card_evidence_for_2024_2025(
    tmp_path: Path,
) -> None:
    """Catches production OCR wiring leaving supported card layouts unavailable."""
    source = tmp_path / "synthetic-layout.pdf"
    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    green = (145 / 255, 198 / 255, 90 / 255)
    document = _approved_segment_document(2025)
    for pdf_page_index in range(1, document.pdf_page_count + 1):
        page = pdf.new_page(width=100.0, height=100.0)
        if pdf_page_index == 7:
            for y in (20.0, 45.0, 55.0, 80.0):
                page.draw_line((10.0, y), (90.0, y), color=green, width=0.75)
    pdf.save(source)  # type: ignore[no-untyped-call]
    pdf.close()  # type: ignore[no-untyped-call]

    class EmptyOcrAdapter:
        def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
            assert image.width > 0 and image.height > 0 and image.rgb_bytes
            return ()

    records = ocr_module.extract_ocr_document(
        source,
        document,
        (7,),
        EmptyOcrAdapter(),
        "sha256:" + "d" * 64,
    )

    assert records[0].status == "extracted"
    assert records[0].raw_page.layout_evidence.status == "detected"
    assert len(records[0].raw_page.layout_evidence.regions) == 2
    assert records[0].layout_segment_provenance is not None
    assert records[0].layout_segment_provenance.detector_version == (
        APPROVED_LAYOUT_DETECTOR_VERSION
    )
    assert records[0].layout_segment_provenance.sampling_status == "sampling_required"
    assert records[0].review_status == "needs_review"


def test_ocr_document_wrapper_revalidates_source_contract_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a forged page-count contract reaching PDF I/O or range expansion."""
    document = _ocr_document(year=2023)
    forged = document.model_construct(
        **{**document.__dict__, "pdf_page_count": 10_001}
    )

    def forbidden_open(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("PDF was opened before source-contract validation")

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    with pytest.raises(
        OcrExtractionError, match="approved document contract is invalid"
    ) as captured:
        ocr_module.extract_ocr_document(
            tmp_path / "must-not-open.pdf",
            forged,
            (1,),
            FakeAdapter(()),
            "sha256:" + "d" * 64,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("document_updates", "page_indexes", "image_digest", "expected_error"),
    [
        ({"extraction_method": "native"}, (1,), "sha256:" + "d" * 64, "contract"),
        ({"edition_year": 2022}, (1,), "sha256:" + "d" * 64, "contract"),
        ({"render_dpi": 350}, (1,), "sha256:" + "d" * 64, "contract"),
        ({}, (1,), "invalid-digest", "image digest"),
        ({}, (), "sha256:" + "d" * 64, "selected pages"),
        ({}, (2,), "sha256:" + "d" * 64, "selected pages"),
        ({}, (1, 1), "sha256:" + "d" * 64, "selected pages"),
    ],
)
def test_ocr_document_wrapper_rejects_unapproved_run_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document_updates: dict[str, object],
    page_indexes: tuple[int, ...],
    image_digest: str,
    expected_error: str,
) -> None:
    """Catches invalid OCR policy and run inputs consuming PDF I/O first."""
    document = _ocr_document(year=2023).model_copy(update=document_updates)

    def forbidden_open(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("PDF was opened before OCR run preflight")

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    with pytest.raises(OcrExtractionError, match=expected_error) as captured:
        ocr_module.extract_ocr_document(
            tmp_path / "must-not-open.pdf",
            document,
            page_indexes,
            FakeAdapter(()),
            image_digest,
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_layout_segment_registry_is_stable_across_geometry_drift_and_source_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches per-page geometry creating strata that can evade reviewed sampling."""

    class DriftingDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def __init__(self) -> None:
            self.calls = 0

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            self.calls += 1
            inset = float(self.calls * 10)
            return (
                ocr_module.RasterLayoutRegion(
                    bbox=(inset, 20.0, image.width - inset, 300.0 + inset)
                ),
            )

    document = _approved_segment_document(2025)
    records = extract_pages(
        tuple(FakePage() for _ in range(document.pdf_page_count)),
        document=document,
        page_indexes=(7, 8),
        adapter=FakeAdapter(()),
        source_sha256=document.sha256,
        image_digest="sha256:" + "d" * 64,
        layout_detector=_trusted_layout_detector(monkeypatch, DriftingDetector()),
    )
    first = _extracted_record(records[0]).layout_segment_provenance
    second = _extracted_record(records[1]).layout_segment_provenance
    assert first is not None and second is not None
    assert first.segment_id == second.segment_id
    assert first.registry_sha256 == second.registry_sha256

    other_document = _approved_segment_document(2024)
    other = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(other_document.pdf_page_count)),
            document=other_document,
            page_indexes=(7,),
            adapter=FakeAdapter(()),
            source_sha256=other_document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(monkeypatch, DriftingDetector()),
        )[0]
    ).layout_segment_provenance
    assert other is not None
    assert other.segment_id != first.segment_id
    assert other.registry_sha256 != first.registry_sha256


def test_approved_2025_body_preserves_registry_segment_without_detected_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches unobserved cards dropping a page from the reviewed sample frame."""

    class EmptyLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return ()

    document = _approved_segment_document(2025)
    record = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(document.pdf_page_count)),
            document=document,
            page_indexes=(7,),
            adapter=FakeAdapter(()),
            source_sha256=document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(
                monkeypatch, EmptyLayoutDetector()
            ),
        )[0]
    )

    assert record.raw_page.layout_evidence.status == "not_detected"
    assert record.layout_segment_provenance is not None
    assert record.layout_segment_provenance.region_count == 0
    assert record.layout_segment_provenance.sampling_status == "sampling_required"
    assert record.review_status == "machine_extracted"


def test_approved_2025_body_rejects_missing_registry_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return ()

    document = _approved_segment_document(2025)
    record = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(document.pdf_page_count)),
            document=document,
            page_indexes=(7,),
            adapter=FakeAdapter(()),
            source_sha256=document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(
                monkeypatch, EmptyLayoutDetector()
            ),
        )[0]
    )
    forged = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, "layout_segment_provenance": None}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged)


def test_2025_page_outside_registry_requires_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return ()

    document = _approved_segment_document(2025)
    record = extract_pages(
        tuple(FakePage() for _ in range(document.pdf_page_count)),
        document=document,
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256=document.sha256,
        image_digest="sha256:" + "d" * 64,
        layout_detector=_trusted_layout_detector(monkeypatch, EmptyLayoutDetector()),
    )[0]

    extracted = _extracted_record(record)
    assert extracted.layout_segment_provenance is None
    assert extracted.review_status == "needs_review"
    assert all(field.review_required for field in extracted.critical_fields)


def test_page_outside_reviewed_layout_registry_has_no_sample_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches front matter being silently enrolled in an approved body sample."""

    class OneRegionDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return (ocr_module.RasterLayoutRegion(bbox=(10.0, 20.0, 900.0, 300.0)),)

    document = _approved_segment_document(2025)
    record = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(document.pdf_page_count)),
            document=document,
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256=document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(monkeypatch, OneRegionDetector()),
        )[0]
    )

    assert record.layout_segment_provenance is None
    assert record.review_status == "needs_review"


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_card_regions_are_derotated_and_resorted_in_pdf_point_order(
    rotation: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches raster order becoming invalid citation order after page derotation."""

    class FractionalLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return (
                ocr_module.RasterLayoutRegion(
                    bbox=(
                        image.width * 0.1,
                        image.height * 0.1,
                        image.width * 0.4,
                        image.height * 0.3,
                    )
                ),
                ocr_module.RasterLayoutRegion(
                    bbox=(
                        image.width * 0.6,
                        image.height * 0.6,
                        image.width * 0.9,
                        image.height * 0.8,
                    )
                ),
            )

    class EmptyOcrAdapter:
        def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
            return ()

    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    page = pdf.new_page(width=200.0, height=100.0)
    page.set_rotation(rotation)  # type: ignore[no-untyped-call]
    try:
        record = extract_pages(
            (page,),
            document=_ocr_document(year=2025),
            page_indexes=(1,),
            adapter=EmptyOcrAdapter(),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(
                monkeypatch, FractionalLayoutDetector()
            ),
        )[0]
    finally:
        pdf.close()  # type: ignore[no-untyped-call]
    record = _extracted_record(record)

    regions = record.raw_page.layout_evidence.regions
    keys = tuple(
        (region.bbox.y0, region.bbox.x0, region.bbox.y1, region.bbox.x1)
        for region in regions
    )
    assert keys == tuple(sorted(keys))
    assert all(
        0.0 <= region.bbox.x0 < region.bbox.x1 <= record.raw_page.page_width
        and 0.0 <= region.bbox.y0 < region.bbox.y1 <= record.raw_page.page_height
        for region in regions
    )


@pytest.mark.parametrize(
    "regions",
    [
        (
            ocr_module.RasterLayoutRegion.model_construct(
                region_type="card",
                bbox=(10.0, 10.0, 10.0, 20.0),
                evidence="raster-border",
            ),
        ),
        (ocr_module.RasterLayoutRegion(bbox=(-1.0, 10.0, 50.0, 20.0)),),
        (ocr_module.RasterLayoutRegion(bbox=(10.0, 10.0, 1001.0, 20.0)),),
        (
            ocr_module.RasterLayoutRegion(bbox=(10.0, 30.0, 50.0, 40.0)),
            ocr_module.RasterLayoutRegion(bbox=(10.0, 10.0, 50.0, 20.0)),
        ),
        (
            ocr_module.RasterLayoutRegion(bbox=(10.0, 10.0, 50.0, 20.0)),
            ocr_module.RasterLayoutRegion(bbox=(10.0, 10.0, 50.0, 20.0)),
        ),
    ],
)
def test_invalid_layout_detector_geometry_is_recorded_as_failed(
    regions: tuple[ocr_module.RasterLayoutRegion, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches malformed detector geometry escaping as trusted card evidence."""

    class InvalidLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return regions

    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2025),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
        layout_detector=_trusted_layout_detector(monkeypatch, InvalidLayoutDetector()),
    )[0]
    record = _extracted_record(record)

    assert record.raw_page.layout_evidence.status == "failed"
    assert record.raw_page.layout_evidence.regions == ()


def test_expected_layout_detector_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches raster detector internals leaking through a fail-closed page record."""

    class FailingLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            raise ocr_module.LayoutDetectionError("PRIVATE LAYOUT DETAIL")

    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2025),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
        layout_detector=_trusted_layout_detector(monkeypatch, FailingLayoutDetector()),
    )[0]
    record = _extracted_record(record)

    assert record.raw_page.layout_evidence.status == "failed"
    assert "PRIVATE" not in str(record.model_dump())


def test_constructed_layout_region_failure_never_serializes_private_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches Pydantic serializer warnings exposing a forged detector region."""
    marker = "SYNTHETIC_PRIVATE_LAYOUT_COORDINATE"

    class ForgedLayoutDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return (
                ocr_module.RasterLayoutRegion.model_construct(
                    region_type="card",
                    bbox=(marker, 10.0, 50.0, 20.0),
                    evidence="raster-border",
                ),
            )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        record = _extracted_record(
            extract_pages(
                (FakePage(),),
                document=_ocr_document(year=2025),
                page_indexes=(1,),
                adapter=FakeAdapter(()),
                source_sha256="c" * 64,
                image_digest="sha256:" + "d" * 64,
                layout_detector=_trusted_layout_detector(
                    monkeypatch, ForgedLayoutDetector()
                ),
            )[0]
        )

    diagnostics = "\n".join(str(item.message) for item in caught)
    assert record.raw_page.layout_evidence.status == "failed"
    assert marker not in diagnostics
    assert caught == []


def test_build_model_preparation_verifies_archive_and_each_file_before_staging(
    tmp_path: Path,
) -> None:
    """Catches a valid archive hash masking a tampered extracted model file."""
    det_archive = _tar_archive("detector", {"inference.pdiparams": b"det-model"})
    rec_archive = _tar_archive("recognizer", {"inference.pdiparams": b"rec-model"})
    payload = _valid_model_lock_payload()
    payload["models"][0]["archive_sha256"] = hashlib.sha256(det_archive).hexdigest()  # type: ignore[index]
    payload["models"][1]["archive_sha256"] = hashlib.sha256(rec_archive).hexdigest()  # type: ignore[index]
    lock = validate_model_lock(payload)
    archives = {
        lock.models[0].source_url: det_archive,
        lock.models[1].source_url: rec_archive,
    }

    prepare_model_staging(lock, tmp_path / "models", archives.__getitem__)

    validate_installed_models(lock, tmp_path / "models")
    assert sorted(
        path.relative_to(tmp_path / "models").as_posix()
        for path in (tmp_path / "models").rglob("*")
        if path.is_file()
    ) == [
        "detector/inference.pdiparams",
        "recognizer/inference.pdiparams",
    ]


def test_build_model_preparation_rejects_archive_hash_and_unsafe_members_without_partial_output(
    tmp_path: Path,
) -> None:
    """Catches unchecked archives escaping staging or leaving partial models behind."""
    payload = _valid_model_lock_payload()
    unsafe_archive = _tar_archive(
        "detector",
        {"inference.pdiparams": b"det-model"},
        unsafe_name="../escape",
    )
    rec_archive = _tar_archive("recognizer", {"inference.pdiparams": b"rec-model"})
    payload["models"][0]["archive_sha256"] = hashlib.sha256(unsafe_archive).hexdigest()  # type: ignore[index]
    payload["models"][1]["archive_sha256"] = hashlib.sha256(rec_archive).hexdigest()  # type: ignore[index]
    lock = validate_model_lock(payload)

    with pytest.raises(ModelLockError, match="archive member"):
        prepare_model_staging(
            lock,
            tmp_path / "models",
            {
                lock.models[0].source_url: unsafe_archive,
                lock.models[1].source_url: rec_archive,
            }.__getitem__,
        )

    assert not (tmp_path / "models").exists()
    assert not (tmp_path / "escape").exists()


def test_ocr_jsonl_is_deterministic_atomic_and_overwrites_stale_content(
    tmp_path: Path,
) -> None:
    """Catches path-dependent output, stale lines, or non-atomic OCR serialization."""
    document = _ocr_document(pages=2)
    image_digest = "sha256:" + "d" * 64
    records = extract_pages(
        (FakePage(), FakePage()),
        document=document,
        page_indexes=(1, 2),
        adapter=FakeAdapter(_fixture_lines("2025-mixed-script.json")),
        source_sha256="c" * 64,
        image_digest=image_digest,
    )
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("STALE PRIVATE CONTENT\n", encoding="utf-8")

    write_ocr_jsonl(
        first,
        records,
        document=document,
        expected_image_digest=image_digest,
        selected_page_indexes=(1, 2),
    )
    write_ocr_jsonl(
        second,
        tuple(reversed(records)),
        document=document,
        expected_image_digest=image_digest,
        selected_page_indexes=(1, 2),
    )

    assert first.read_bytes() == second.read_bytes()
    assert b"STALE PRIVATE CONTENT" not in first.read_bytes()
    assert [
        json.loads(line)["pdf_page_index"]
        for line in first.read_text(encoding="utf-8").splitlines()
    ] == [1, 2]
    assert all(
        json.loads(line)["schema_version"] == 2
        for line in first.read_text(encoding="utf-8").splitlines()
    )


def test_ocr_writer_rejects_records_from_mixed_document_runs(tmp_path: Path) -> None:
    """Catches one selected-page JSONL mixing unrelated document provenance."""
    first_document = _ocr_document(year=2023, pages=2)
    second_document = SourceDocument.model_validate(
        {**first_document.model_dump(), "doc_id": "second-synthetic-document"}
    )
    pages = (FakePage(), FakePage())
    first_record = extract_pages(
        pages,
        document=first_document,
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256=first_document.sha256,
        image_digest="sha256:" + "d" * 64,
    )[0]
    second_record = extract_pages(
        pages,
        document=second_document,
        page_indexes=(2,),
        adapter=FakeAdapter(()),
        source_sha256=second_document.sha256,
        image_digest="sha256:" + "d" * 64,
    )[0]

    with pytest.raises(OcrExtractionError, match="approved run"):
        write_ocr_jsonl(
            tmp_path / "mixed.jsonl",
            (first_record, second_record),
            document=first_document,
            expected_image_digest="sha256:" + "d" * 64,
            selected_page_indexes=(1, 2),
        )


def test_ocr_writer_binds_source_image_and_sparse_selection_contract(
    tmp_path: Path,
) -> None:
    """Catches a self-consistent selected run being rebound to other provenance."""
    document = _ocr_document(year=2023, pages=3)
    image_digest = "sha256:" + "d" * 64
    records = extract_pages(
        (FakePage(), FakePage(), FakePage()),
        document=document,
        page_indexes=(1, 3),
        adapter=FakeAdapter(()),
        source_sha256=document.sha256,
        image_digest=image_digest,
    )

    write_ocr_jsonl(
        tmp_path / "matching-sparse.jsonl",
        records,
        document=document,
        expected_image_digest=image_digest,
        selected_page_indexes=(1, 3),
    )

    source_rebound = tuple(
        type(record).model_validate({**record.model_dump(), "source_sha256": "e" * 64})
        for record in records
    )
    with pytest.raises(OcrExtractionError, match="approved run"):
        write_ocr_jsonl(
            tmp_path / "source-rebound.jsonl",
            source_rebound,
            document=document,
            expected_image_digest=image_digest,
            selected_page_indexes=(1, 3),
        )

    image_rebound = tuple(
        type(record).model_validate(
            {**record.model_dump(), "image_digest": "sha256:" + "e" * 64}
        )
        for record in records
    )
    with pytest.raises(OcrExtractionError, match="approved run"):
        write_ocr_jsonl(
            tmp_path / "image-rebound.jsonl",
            image_rebound,
            document=document,
            expected_image_digest=image_digest,
            selected_page_indexes=(1, 3),
        )

    with pytest.raises(OcrExtractionError, match="selected pages"):
        write_ocr_jsonl(
            tmp_path / "selection-rebound.jsonl",
            records,
            document=document,
            expected_image_digest=image_digest,
            selected_page_indexes=(1, 2, 3),
        )


def test_ocr_writer_recursively_revalidates_document_contract_without_value_leak(
    tmp_path: Path,
) -> None:
    """Catches nested model_construct bypasses in an OCR document contract."""
    document = _ocr_document(year=2023)
    image_digest = "sha256:" + "d" * 64
    records = extract_pages(
        (FakePage(),),
        document=document,
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256=document.sha256,
        image_digest=image_digest,
    )
    marker = "SYNTHETIC_PRIVATE_OCR_DOCUMENT"
    profile = document.page_size_profiles[0]
    forged_profile = type(profile).model_construct(
        **{**profile.__dict__, "end_pdf_page": 2}
    )
    forged_document = SourceDocument.model_construct(
        **{
            **document.__dict__,
            "publisher": marker,
            "page_size_profiles": (forged_profile,),
        }
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OcrExtractionError, match="contract is invalid") as error:
            write_ocr_jsonl(
                tmp_path / "forged-document.jsonl",
                records,
                document=forged_document,
                expected_image_digest=image_digest,
                selected_page_indexes=(1,),
            )

    diagnostics = "\n".join(
        (
            str(error.value),
            repr(error.value),
            "".join(traceback.format_exception(error.value)),
            *(str(item.message) for item in caught),
        )
    )
    assert marker not in diagnostics
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert caught == []


@pytest.mark.parametrize(
    "forged_target", ["raw-span", "layout-evidence", "review-entry", "critical-field"]
)
def test_ocr_writer_recursively_revalidates_constructed_models_without_value_leak(
    tmp_path: Path, forged_target: str
) -> None:
    marker = f"SYNTHETIC_PRIVATE_OCR_{forged_target.upper()}"
    document = _ocr_document(year=2025)
    image_digest = "sha256:" + "d" * 64
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=document,
            page_indexes=(1,),
            adapter=FakeAdapter(
                (
                    AdapterLine(
                        text="synthetic question",
                        bbox=(10.0, 20.0, 300.0, 60.0),
                        confidence=0.1,
                        field_type="question",
                    ),
                )
            ),
            source_sha256="c" * 64,
            image_digest=image_digest,
        )[0]
    )
    record_fields = dict(record.__dict__)
    if forged_target == "raw-span":
        raw_block = record.raw_page.raw_blocks[0]
        raw_line = raw_block.lines[0]
        raw_span = raw_line.spans[0]
        forged_span = RawSpan.model_construct(
            **{**raw_span.__dict__, "font": marker, "size": float("inf")}
        )
        forged_line = RawLine.model_construct(
            **{**raw_line.__dict__, "spans": (forged_span,)}
        )
        forged_block = RawBlock.model_construct(
            **{**raw_block.__dict__, "lines": (forged_line,)}
        )
        record_fields["raw_page"] = RawPage.model_construct(
            **{**record.raw_page.__dict__, "raw_blocks": (forged_block,)}
        )
    elif forged_target == "layout-evidence":
        forged_layout = common_models.LayoutEvidence.model_construct(
            status="unavailable", detector_version=marker, regions=()
        )
        record_fields["raw_page"] = RawPage.model_construct(
            **{**record.raw_page.__dict__, "layout_evidence": forged_layout}
        )
    elif forged_target == "review-entry":
        review = record.review_queue[0]
        record_fields["review_queue"] = (
            type(review).model_construct(**{**review.__dict__, "location_id": marker}),
        )
    else:
        critical = record.critical_fields[0]
        record_fields["critical_fields"] = (
            type(critical).model_construct(
                **{**critical.__dict__, "field_type": marker}
            ),
            *record.critical_fields[1:],
        )
    forged_record = ExtractedOcrPageRecord.model_construct(**record_fields)

    with pytest.raises(OcrExtractionError, match="record is invalid") as direct:
        validate_ocr_page_record(forged_record)
    assert direct.value.__cause__ is None
    assert direct.value.__context__ is None

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OcrExtractionError, match="record is invalid") as error:
            write_ocr_jsonl(
                tmp_path / "forged.jsonl",
                (forged_record,),
                document=document,
                expected_image_digest=image_digest,
                selected_page_indexes=(1,),
            )

    diagnostics = "\n".join(
        (
            str(error.value),
            repr(error.value),
            "".join(traceback.format_exception(error.value)),
            *(str(item.message) for item in caught),
        )
    )
    assert marker not in diagnostics
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert caught == []


def test_ocr_writer_revalidates_constructed_layout_segment_without_value_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "SYNTHETIC_PRIVATE_OCR_LAYOUT_SEGMENT"

    class OneRegionDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return (ocr_module.RasterLayoutRegion(bbox=(10.0, 10.0, 900.0, 300.0)),)

    document = _approved_segment_document(2025)
    record = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(document.pdf_page_count)),
            document=document,
            page_indexes=(7,),
            adapter=FakeAdapter(()),
            source_sha256=document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(monkeypatch, OneRegionDetector()),
        )[0]
    )
    segment = record.layout_segment_provenance
    assert segment is not None
    forged_segment = type(segment).model_construct(
        **{**segment.__dict__, "registry_sha256": marker}
    )
    forged_record = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, "layout_segment_provenance": forged_segment}
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(OcrExtractionError, match="record is invalid") as error:
            write_ocr_jsonl(
                tmp_path / "forged-segment.jsonl",
                (forged_record,),
                document=document,
                expected_image_digest="sha256:" + "d" * 64,
                selected_page_indexes=(7,),
            )

    diagnostics = "\n".join(
        (
            str(error.value),
            repr(error.value),
            "".join(traceback.format_exception(error.value)),
            *(str(item.message) for item in caught),
        )
    )
    assert marker not in diagnostics
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert caught == []


def test_ocr_record_rejects_missing_deterministic_review_entry() -> None:
    """Catches a valid-shaped record dropping a required low-confidence review."""
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=FakeAdapter(
                (
                    AdapterLine(
                        text="synthetic question",
                        bbox=(10.0, 20.0, 300.0, 60.0),
                        confidence=0.1,
                        field_type="question",
                    ),
                )
            ),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    assert len(record.review_queue) == 1
    forged = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, "review_queue": ()}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("source_sha256", "not-a-digest"),
        ("render_dpi", -1),
        ("image_digest", "not-an-image-digest"),
        ("quality_flags", ("fabricated",)),
    ],
)
def test_ocr_record_rejects_noncanonical_run_provenance(
    field_name: str, forged_value: object
) -> None:
    """Catches valid-shaped records rebinding reviewed annual run metadata."""
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    forged = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, field_name: forged_value}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged)


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("source_sha256", "not-a-digest"),
        ("render_dpi", -1),
        ("image_digest", "not-an-image-digest"),
        ("quality_flags", ("fabricated",)),
    ],
)
def test_quarantined_ocr_record_rejects_noncanonical_run_provenance(
    field_name: str, forged_value: object
) -> None:
    """Catches quarantine envelopes escaping the same annual run contract."""
    record = _quarantined_record(
        extract_pages(
            (FakePage(failure=OSError("synthetic render failure")),),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    forged = QuarantinedOcrPageRecord.model_construct(
        **{**record.__dict__, field_name: forged_value}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged)


@pytest.mark.parametrize("failure_stage", ["render", "adapter"])
def test_quarantined_ocr_record_binds_render_hash_to_failure_stage(
    failure_stage: str,
) -> None:
    """Catches quarantine reasons being rebound across the raster boundary."""
    if failure_stage == "render":
        page = FakePage(failure=OSError("synthetic render failure"))
        adapter = FakeAdapter(())
        forged_render_sha256: str | None = "a" * 64
    else:
        page = FakePage()
        adapter = FakeAdapter((), failing_calls=frozenset({1}))
        forged_render_sha256 = None
    record = _quarantined_record(
        extract_pages(
            (page,),
            document=_ocr_document(year=2023),
            page_indexes=(1,),
            adapter=adapter,
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    forged = QuarantinedOcrPageRecord.model_construct(
        **{**record.__dict__, "render_sha256": forged_render_sha256}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged)


def test_ocr_record_rejects_valid_but_non_fail_closed_critical_status() -> None:
    record = _extracted_record(
        extract_pages(
            (FakePage(),),
            document=_ocr_document(year=2024),
            page_indexes=(1,),
            adapter=FakeAdapter(()),
            source_sha256="c" * 64,
            image_digest="sha256:" + "d" * 64,
        )[0]
    )
    critical = record.critical_fields[0]
    forged_critical = type(critical).model_validate(
        {**critical.model_dump(), "review_required": False}
    )
    forged_record = ExtractedOcrPageRecord.model_construct(
        **{
            **record.__dict__,
            "critical_fields": (forged_critical, *record.critical_fields[1:]),
        }
    )

    with pytest.raises(OcrExtractionError, match="record is invalid"):
        validate_ocr_page_record(forged_record)


def test_ocr_writer_rejects_layout_segment_page_mapping_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OneRegionDetector:
        version = APPROVED_LAYOUT_DETECTOR_VERSION

        def detect(
            self, image: RasterImage
        ) -> tuple[ocr_module.RasterLayoutRegion, ...]:
            return (ocr_module.RasterLayoutRegion(bbox=(10.0, 20.0, 900.0, 300.0)),)

    document = _approved_segment_document(2025)
    record = _extracted_record(
        extract_pages(
            tuple(FakePage() for _ in range(document.pdf_page_count)),
            document=document,
            page_indexes=(7,),
            adapter=FakeAdapter(()),
            source_sha256=document.sha256,
            image_digest="sha256:" + "d" * 64,
            layout_detector=_trusted_layout_detector(monkeypatch, OneRegionDetector()),
        )[0]
    )
    segment = record.layout_segment_provenance
    assert segment is not None
    alternate_end = segment.segment_end_pdf_page + 1
    registry_payload = {
        "detector_version": APPROVED_LAYOUT_DETECTOR_VERSION,
        "doc_id": record.doc_id,
        "edition_year": record.edition_year,
        "policy_version": segment.registry_policy_version,
        "sampling_status": segment.sampling_status,
        "segment_end_pdf_page": alternate_end,
        "segment_key": segment.segment_key,
        "segment_start_pdf_page": segment.segment_start_pdf_page,
        "source_sha256": record.source_sha256,
    }
    registry_sha256 = hashlib.sha256(
        b"sen-qa-layout-segment-registry-v1\0"
        + json.dumps(
            registry_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    forged_segment = type(segment).model_validate(
        {
            **segment.model_dump(),
            "segment_end_pdf_page": alternate_end,
            "registry_sha256": registry_sha256,
            "segment_id": "layout-segment-"
            + hashlib.sha256(
                b"sen-qa-layout-segment-id-v1\0" + registry_sha256.encode("ascii")
            ).hexdigest()[:32],
        }
    )
    forged_record = ExtractedOcrPageRecord.model_construct(
        **{**record.__dict__, "layout_segment_provenance": forged_segment}
    )

    with pytest.raises(OcrExtractionError, match="record is invalid") as error:
        write_ocr_jsonl(
            tmp_path / "forged-segment-range.jsonl",
            (forged_record,),
            document=document,
            expected_image_digest="sha256:" + "d" * 64,
            selected_page_indexes=(7,),
        )

    assert error.value.__cause__ is None
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    ("selection", "expected"),
    [("13", (13,)), ("1-3,5", (1, 2, 3, 5)), ("2,4-5", (2, 4, 5))],
)
def test_ocr_page_selection_accepts_only_explicit_positive_ranges(
    selection: str, expected: tuple[int, ...]
) -> None:
    """Catches a range parser skipping or reordering explicitly selected pages."""
    assert _parse_ocr_pages(selection) == expected


@pytest.mark.parametrize("selection", ["", "0", "2-1", "1,1", "1-2,2", " 1", "1-"])
def test_ocr_page_selection_rejects_ambiguous_or_duplicate_ranges(
    selection: str,
) -> None:
    """Catches ambiguous CLI page syntax reaching PDF indexing."""
    with pytest.raises(ValueError, match="pages"):
        _parse_ocr_pages(selection)


def test_ocr_page_selection_accepts_supported_full_range() -> None:
    """Catches an off-by-one at the central page-selection safety ceiling."""
    pages = _parse_ocr_pages("1-10000")

    assert len(pages) == 10_000
    assert pages[0] == 1
    assert pages[-1] == 10_000


@pytest.mark.parametrize("selection", ["1-10001", "1-999999"])
def test_ocr_page_selection_rejects_large_endpoint_before_range_expansion(
    selection: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches oversized endpoints reaching list/tuple range expansion."""

    def range_bomb(*args: object) -> range:
        del args
        raise AssertionError("oversized CLI page range was expanded")

    monkeypatch.setattr(cli_module, "range", range_bomb, raising=False)
    with pytest.raises(ValueError, match="pages"):
        _parse_ocr_pages(selection)


def test_ocr_page_selection_rejects_overlong_syntax_before_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unbounded page-selection string reaching regex scanning."""

    def fullmatch_bomb(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("overlong CLI page syntax reached the regex engine")

    monkeypatch.setattr(cli_module.re, "fullmatch", fullmatch_bomb)
    with pytest.raises(ValueError, match="pages"):
        _parse_ocr_pages("1" * 120_001)


class _PageSelectionSplitBomb(str):
    def split(self, *args: object, **kwargs: object) -> list[str]:
        del args, kwargs
        raise AssertionError("excess CLI page intervals were split")


def test_ocr_page_selection_rejects_too_many_intervals_before_split() -> None:
    """Catches tokenizing more page intervals than the corpus can contain."""
    selection = _PageSelectionSplitBomb(",".join("1" for _ in range(10_001)))

    with pytest.raises(ValueError, match="pages"):
        _parse_ocr_pages(selection)


def test_extract_ocr_cli_rejects_small_argv_huge_range_without_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a short malicious --pages range escaping the fixed CLI boundary."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    rejected_endpoint = "50000000"

    def range_bomb(*args: object) -> range:
        del args
        raise AssertionError("malicious CLI page range was expanded")

    monkeypatch.setattr(cli_module, "range", range_bomb, raising=False)
    result = CliRunner().invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2025",
            "--pages",
            f"1-{rejected_endpoint}",
            "--output",
            str(tmp_path / "output"),
        ],
        env={
            "SEN_QA_SOURCE_ROOT": str(source_root),
            "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
        },
    )

    assert result.exit_code == 2
    assert "pages" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr
    assert rejected_endpoint not in result.stdout + result.stderr


def test_extract_ocr_cli_requires_source_root_and_exact_image_digest(
    tmp_path: Path,
) -> None:
    """Catches an unverified source or untraceable image entering an OCR run."""
    runner = CliRunner()
    missing_source = runner.invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2025",
            "--pages",
            "1",
            "--output",
            str(tmp_path / "out"),
        ],
        env={"SEN_QA_SOURCE_ROOT": "", "SEN_QA_INGESTION_IMAGE_DIGEST": ""},
    )
    missing_digest = runner.invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2025",
            "--pages",
            "1",
            "--output",
            str(tmp_path / "out"),
        ],
        env={"SEN_QA_SOURCE_ROOT": str(tmp_path), "SEN_QA_INGESTION_IMAGE_DIGEST": ""},
    )

    assert missing_source.exit_code == 2
    assert "SEN_QA_SOURCE_ROOT is required" in missing_source.stdout
    assert missing_digest.exit_code == 2
    assert "SEN_QA_INGESTION_IMAGE_DIGEST is required" in missing_digest.stdout
    assert "Traceback" not in missing_source.stdout + missing_digest.stdout


def test_extract_ocr_cli_rejects_native_year_and_bad_pages_without_traceback(
    tmp_path: Path,
) -> None:
    """Catches native PDFs or unsafe page syntax crossing the OCR-only boundary."""
    runner = CliRunner()
    environment = {
        "SEN_QA_SOURCE_ROOT": str(tmp_path),
        "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
    }
    native = runner.invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2022",
            "--pages",
            "1",
            "--output",
            str(tmp_path.parent / "out-native"),
        ],
        env=environment,
    )
    bad_pages = runner.invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2025",
            "--pages",
            "1-",
            "--output",
            str(tmp_path.parent / "out-pages"),
        ],
        env=environment,
    )

    assert native.exit_code == 2
    assert "not approved for OCR" in native.stdout
    assert bad_pages.exit_code == 2
    assert "pages" in bad_pages.stdout
    assert "Traceback" not in native.stdout + bad_pages.stdout


def test_extract_ocr_cli_verifies_then_atomically_writes_count_only_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches runtime download fallback, unsafe promotion, or OCR text leaking to stdout."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "2025.pdf"
    source.write_bytes(b"synthetic source placeholder")
    output = tmp_path / "output"
    output.mkdir()
    (output / "fixture-2025.jsonl").write_text(
        "STALE PRIVATE CONTENT\n", encoding="utf-8"
    )
    document = _ocr_document()
    records = extract_pages(
        (FakePage(),),
        document=document,
        page_indexes=(1,),
        adapter=FakeAdapter(
            (
                AdapterLine(
                    text="PRIVATE OCR CONTENT",
                    bbox=(10.0, 20.0, 300.0, 60.0),
                    confidence=0.95,
                ),
            )
        ),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )

    _patch_successful_ocr_cli(
        monkeypatch, source=source, document=document, records=records
    )

    result = CliRunner().invoke(
        app,
        ["extract-ocr", "--year", "2025", "--pages", "1", "--output", str(output)],
        env={
            "SEN_QA_SOURCE_ROOT": str(source_root),
            "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
        },
    )

    assert result.exit_code == 0
    assert (
        result.stdout.strip()
        == "documents=1 pages=1 extracted=1 quarantined=0 failed=0"
    )
    assert "PRIVATE OCR CONTENT" not in result.stdout
    assert b"STALE PRIVATE CONTENT" not in (output / "fixture-2025.jsonl").read_bytes()
    payload = json.loads((output / "fixture-2025.jsonl").read_text(encoding="utf-8"))
    assert (
        payload["raw_page"]["raw_blocks"][0]["lines"][0]["spans"][0]["text"]
        == "PRIVATE OCR CONTENT"
    )


def test_extract_ocr_cli_writes_atomically_inside_mount_when_parent_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches creating a sibling promotion workspace on the read-only container root."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "2025.pdf"
    source.write_bytes(b"synthetic source placeholder")
    runtime_artifacts = tmp_path / "runtime-artifacts"
    runtime_artifacts.mkdir()
    output = runtime_artifacts / "ocr-smoke"
    output.mkdir()
    document = _ocr_document()
    records = extract_pages(
        (FakePage(),),
        document=document,
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )
    _patch_successful_ocr_cli(
        monkeypatch, source=source, document=document, records=records
    )

    runtime_artifacts.chmod(0o555)
    try:
        result = CliRunner().invoke(
            app,
            [
                "extract-ocr",
                "--year",
                "2025",
                "--pages",
                "1",
                "--output",
                str(output),
            ],
            env={
                "SEN_QA_SOURCE_ROOT": str(source_root),
                "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
            },
        )
    finally:
        runtime_artifacts.chmod(0o755)

    assert result.exit_code == 0
    assert (output / "fixture-2025.jsonl").is_file()
    assert list(runtime_artifacts.iterdir()) == [output]


def test_extract_ocr_cli_preserves_and_rejects_unmanaged_output_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches mounted-directory extraction deleting or silently retaining unknown artifacts."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "2025.pdf"
    source.write_bytes(b"synthetic source placeholder")
    output = tmp_path / "ocr-smoke"
    output.mkdir()
    unmanaged = output / "operator-note.txt"
    unmanaged.write_text("preserve me", encoding="utf-8")
    document = _ocr_document()
    records = extract_pages(
        (FakePage(),),
        document=document,
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )
    _patch_successful_ocr_cli(
        monkeypatch, source=source, document=document, records=records
    )

    result = CliRunner().invoke(
        app,
        ["extract-ocr", "--year", "2025", "--pages", "1", "--output", str(output)],
        env={
            "SEN_QA_SOURCE_ROOT": str(source_root),
            "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
        },
    )

    assert result.exit_code == 1
    assert "unmanaged output file" in result.stdout
    assert unmanaged.read_text(encoding="utf-8") == "preserve me"
    assert not (output / "fixture-2025.jsonl").exists()


def test_runtime_cli_does_not_expose_build_only_model_preparation(
    tmp_path: Path,
) -> None:
    """Catches a network-capable model downloader remaining callable in the final image."""
    result = CliRunner().invoke(
        app,
        ["prepare-ocr-models", "--help"],
    )

    assert result.exit_code == 2
    assert "No such command" in result.output
    assert not (tmp_path / "models").exists()


def test_extract_ocr_cli_fails_closed_on_missing_models_before_adapter_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches runtime fallback download or Paddle initialization after local model failure."""
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "2025.pdf"
    source.write_bytes(b"synthetic source placeholder")
    model_root = tmp_path / "empty-models"
    model_root.mkdir()
    output = tmp_path / "ocr-smoke"
    document = _ocr_document()
    adapter_initialized = False

    monkeypatch.setattr(cli_module, "load_manifest", lambda path: (document,))
    monkeypatch.setattr(cli_module, "resolve_source", lambda root, selected: source)
    monkeypatch.setattr(cli_module, "verify_source", lambda path, selected: None)

    def fail_if_initialized(lock: object, root: Path) -> FakeAdapter:
        nonlocal adapter_initialized
        adapter_initialized = True
        return FakeAdapter(())

    monkeypatch.setattr(cli_module, "create_paddle_adapter", fail_if_initialized)

    result = CliRunner().invoke(
        app,
        [
            "extract-ocr",
            "--year",
            "2025",
            "--pages",
            "1",
            "--output",
            str(output),
            "--model-root",
            str(model_root),
        ],
        env={
            "SEN_QA_SOURCE_ROOT": str(source_root),
            "SEN_QA_INGESTION_IMAGE_DIGEST": "sha256:" + "d" * 64,
        },
    )

    assert result.exit_code == 1
    assert "missing locked model file" in result.stdout
    assert adapter_initialized is False
    assert not output.exists()
