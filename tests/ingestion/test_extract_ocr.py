"""Behavior contracts for offline, provenance-preserving OCR extraction."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path

import pymupdf
import pytest
from typer.testing import CliRunner

import src.cli as cli_module
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
    RasterImage,
    extract_pages,
    load_model_lock,
    ocr_policy,
    prepare_model_staging,
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
        "packages": {"paddleocr": "3.7.0", "paddlepaddle": "3.3.1"},
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
    width = 1000
    height = 1400
    n = 3
    samples = bytes(range(12))


class FakePage:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[dict[str, object]] = []

    def get_pixmap(self, **kwargs: object) -> FakePixmap:
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return FakePixmap()


class FakeAdapter:
    def __init__(
        self,
        lines: tuple[AdapterLine, ...],
        *,
        failing_calls: frozenset[int] = frozenset(),
    ) -> None:
        self.lines = lines
        self.failing_calls = failing_calls
        self.calls = 0

    def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
        assert image.width == 1000
        assert image.height == 1400
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
    lock = load_model_lock(Path("config/models.lock.json"))

    assert lock.language == "korean"
    assert lock.packages.paddleocr == "3.7.0"
    assert lock.packages.paddlepaddle == "3.3.1"
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
    (output / "stale.txt").write_text("stale", encoding="utf-8")
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
    assert not (output / "stale.txt").exists()
    payload = json.loads((output / "fixture-2025.jsonl").read_text(encoding="utf-8"))
    assert (
        payload["raw_page"]["raw_blocks"][0]["lines"][0]["spans"][0]["text"]
        == "PRIVATE OCR CONTENT"
    )
