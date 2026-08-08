"""Behavior contracts for offline, provenance-preserving OCR extraction."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tomllib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest
from typer.testing import CliRunner

import src.cli as cli_module
import src.ingestion.extract_ocr as ocr_module
from docker.prepare_ocr_models import prepare_model_staging
from src.cli import _parse_ocr_pages, app
from src.ingestion.extract_common import (
    BoundingBox,
    RawBlock,
    RawLine,
    RawPage,
    RawSpan,
)
from src.ingestion.extract_ocr import (
    AdapterLine,
    ModelLockError,
    OcrAdapterError,
    PaddleOcrAdapter,
    RasterImage,
    extract_pages,
    load_model_lock,
    ocr_policy,
    sort_reading_order,
    validate_installed_models,
    validate_model_lock,
    write_ocr_jsonl,
)
from src.ingestion.manifest import SourceDocument


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


def _fixture_lines(name: str) -> tuple[AdapterLine, ...]:
    payload = json.loads(
        (Path("tests/fixtures/ocr-pages") / name).read_text(encoding="utf-8")
    )
    return tuple(
        AdapterLine.model_validate({**line, "bbox": tuple(line["bbox"])})
        for line in payload["lines"]
    )


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


def test_checked_in_model_lock_matches_frozen_runtime_packages() -> None:
    """Catches model metadata drifting from the exact uv.lock/runtime packages."""
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))
    lock = load_model_lock(Path("config/models.lock.json"))
    paddle_package = next(
        package
        for package in uv_lock["package"]
        if package["name"] == "paddlepaddle"
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
    assert {
        key: cp311_linux_wheel[key] for key in ("url", "hash", "size")
    } == {
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
            lambda payload: payload["packages"].update(paddleocr="3.6.0"),  # type: ignore[union-attr]
            "package versions",
        ),
        (lambda payload: payload["models"][0].update(revision=""), "revision"),  # type: ignore[index,union-attr]
        (lambda payload: payload["models"][0].update(revision="latest"), "revision"),  # type: ignore[index,union-attr]
        (
            lambda payload: payload["models"][0].update(
                source_url="https://example.invalid/model.tar"
            ),  # type: ignore[index,union-attr]
            "source",
        ),
        (
            lambda payload: payload["models"][0].update(archive_sha256="A" * 64),
            "SHA-256",
        ),  # type: ignore[index,union-attr]
        (
            lambda payload: payload["models"][0]["files"][0].update(path="../escape"),
            "path",
        ),  # type: ignore[index,union-attr]
        (
            lambda payload: payload["models"][0]["files"].append(
                deepcopy(payload["models"][0]["files"][0])
            ),
            "duplicate",
        ),  # type: ignore[index,union-attr]
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
        ocr_module.importlib,
        "import_module",
        lambda name: fake_numpy
        if name == "numpy"
        else SimpleNamespace(PaddleOCR=CapturingPaddleOcr),
    )
    detection_model = tmp_path / "PP-OCRv5_server_det_infer"
    recognition_model = tmp_path / "korean_PP-OCRv5_mobile_rec_infer"

    adapter = PaddleOcrAdapter(
        detection_model=detection_model, recognition_model=recognition_model
    )
    lines = adapter.recognize(
        RasterImage(width=1, height=1, rgb_bytes=b"\x01\x02\x03")
    )

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
    assert predicted_images[0].tolist() == [[[3, 2, 1]]]  # type: ignore[union-attr]


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
        adapter=FakeAdapter(
            (line_300,), expected_width=600, expected_height=300
        ),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]
    record_350 = extract_pages(
        (page_350,),
        document=_ocr_document(year=2024),
        page_indexes=(1,),
        adapter=FakeAdapter(
            (line_350,), expected_width=700, expected_height=350
        ),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]

    bbox_300 = record_300.raw_page.raw_blocks[0].lines[0].bbox
    bbox_350 = record_350.raw_page.raw_blocks[0].lines[0].bbox
    assert bbox_300 == bbox_350 == BoundingBox(
        x0=20.0, y0=10.0, x1=100.0, y1=50.0
    )
    assert record_300.review_queue[0].location_id == record_350.review_queue[0].location_id


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

    pdf = pymupdf.open()
    page = pdf.new_page(width=200.0, height=100.0)
    page.set_rotation(rotation)
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
        pdf.close()

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


def test_2025_records_explicit_sample_and_layout_escalation_policy() -> None:
    """Catches 2025 machine output being treated as answer-approved by omission."""
    record = extract_pages(
        (FakePage(),),
        document=_ocr_document(year=2025),
        page_indexes=(1,),
        adapter=FakeAdapter(()),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )[0]

    assert record.critical_review_policy == "stratified-sample-with-layout-escalation"
    assert all(field.status == "sampling_required" for field in record.critical_fields)
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
    assert records[0].reason_code == "ocr-adapter-failed"
    assert records[0].render_sha256 is not None
    assert "PRIVATE" not in str(records[0].model_dump())


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
    assert records[0].reason_code == "page-render-failed"
    assert "PRIVATE" not in str(records[0].model_dump())


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
    assert records[0].reason_code == "page-render-failed"


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
    records = extract_pages(
        (FakePage(), FakePage()),
        document=_ocr_document(pages=2),
        page_indexes=(1, 2),
        adapter=FakeAdapter(_fixture_lines("2025-mixed-script.json")),
        source_sha256="c" * 64,
        image_digest="sha256:" + "d" * 64,
    )
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("STALE PRIVATE CONTENT\n", encoding="utf-8")

    write_ocr_jsonl(first, records)
    write_ocr_jsonl(second, tuple(reversed(records)))

    assert first.read_bytes() == second.read_bytes()
    assert b"STALE PRIVATE CONTENT" not in first.read_bytes()
    assert [
        json.loads(line)["pdf_page_index"]
        for line in first.read_text(encoding="utf-8").splitlines()
    ] == [1, 2]


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
    assert b"STALE PRIVATE CONTENT" not in (
        output / "fixture-2025.jsonl"
    ).read_bytes()
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


def test_runtime_cli_does_not_expose_build_only_model_preparation(tmp_path: Path) -> None:
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
