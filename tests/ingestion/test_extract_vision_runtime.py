"""Runtime-bound document extraction for local Apple Vision OCR."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PosixPath
from types import SimpleNamespace

import pymupdf
import pytest

import src.ingestion.extract_ocr as ocr_module
from src.ingestion.manifest import SourceDocument, load_manifest

_CURRENT_PIPELINE = object()


class _HostilePath(PosixPath):
    def __fspath__(self) -> str:
        raise RuntimeError("PRIVATE_SOURCE_PATH_BOMB")


def _document(year: int = 2024) -> SourceDocument:
    return next(
        document
        for document in load_manifest(Path("data/manifests/sen_qa_sources.json"))
        if document.edition_year == year
    )


def _runtime_payload(
    *,
    helper_binary: str = "2" * 64,
    extractor_pipeline: object = _CURRENT_PIPELINE,
) -> dict[str, object]:
    return {
        "schema_version": "sen-qa-apple-vision-runtime-provenance/v2",
        "engine": "apple-vision",
        "request_revision": 3,
        "language": "ko-KR",
        "recognition_level": "accurate",
        "uses_language_correction": True,
        "macos_build": "25E5206a",
        "architecture": "arm64",
        "swift_version": "6.3.2",
        "sdk_version": "macosx26.4",
        "helper_source_sha256": "1" * 64,
        "helper_binary_sha256": helper_binary,
        "adapter_sha256": "3" * 64,
        "extractor_pipeline_sha256": (
            hashlib.sha256(Path(ocr_module.__file__).read_bytes()).hexdigest()
            if extractor_pipeline is _CURRENT_PIPELINE
            else extractor_pipeline
        ),
        "pymupdf_version": "1.26.7",
    }


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _runtime(
    *, helper_binary: str = "2" * 64
) -> ocr_module.AppleVisionRuntimeProvenance:
    return ocr_module.build_apple_vision_runtime_provenance(
        _canonical_bytes(_runtime_payload(helper_binary=helper_binary))
    )


class _Pixmap:
    width = 2
    height = 2
    n = 3
    samples = b"\x00" * 12


class _Page:
    rect = SimpleNamespace(
        x0=0.0,
        y0=0.0,
        x1=594.9920043945312,
        y1=841.9920043945312,
        width=594.9920043945312,
        height=841.9920043945312,
    )
    derotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def get_pixmap(self, **kwargs: object) -> _Pixmap:
        assert kwargs == {"dpi": 350, "alpha": False}
        return _Pixmap()


class _Pdf:
    def __init__(self, page_count: int, *, close_failure: bool = False) -> None:
        self.page_count = page_count
        self.close_failure = close_failure
        self.closed = False

    def __len__(self) -> int:
        return self.page_count

    def __getitem__(self, _: int) -> _Page:
        return _Page()

    def close(self) -> None:
        if self.close_failure:
            raise OSError("SYNTHETIC_PRIVATE_PDF_CLOSE_FAILURE")
        self.closed = True


class _Adapter:
    def __init__(self, runtime_bytes: bytes) -> None:
        self.runtime_bytes = runtime_bytes

    def complete_runtime_provenance_bytes(self) -> bytes:
        return self.runtime_bytes

    def recognize(
        self, image: ocr_module.RasterImage
    ) -> tuple[ocr_module.AdapterLine, ...]:
        assert image == ocr_module.RasterImage(2, 2, b"\x00" * 12)
        return (
            ocr_module.AdapterLine(
                text="질문 예시",
                bbox=(0.0, 0.0, 1.0, 1.0),
                confidence=0.99,
                field_type="question",
            ),
        )


class _FailingAdapter(_Adapter):
    def recognize(
        self, image: ocr_module.RasterImage
    ) -> tuple[ocr_module.AdapterLine, ...]:
        raise ocr_module.OcrAdapterError("SYNTHETIC_PRIVATE_HELPER_FAILURE")


class _ListAdapter(_Adapter):
    def recognize(  # type: ignore[override]
        self, image: ocr_module.RasterImage
    ) -> list[ocr_module.AdapterLine]:
        return list(super().recognize(image))


def _assert_value_free(error: BaseException, message: str) -> None:
    assert str(error) == message
    assert error.__cause__ is None
    assert error.__context__ is None


def _write_pdf(path: Path, *, page_count: int, title: str) -> None:
    pdf = pymupdf.open()  # type: ignore[no-untyped-call]
    for _ in range(page_count):
        pdf.new_page(width=100.0, height=100.0)
    pdf.set_metadata({"title": title})
    pdf.save(path)  # type: ignore[no-untyped-call]
    pdf.close()  # type: ignore[no-untyped-call]


def _document_with_source_sha256(
    monkeypatch: pytest.MonkeyPatch, source_sha256: str
) -> SourceDocument:
    document = _document().model_copy(update={"sha256": source_sha256})
    registry = dict(ocr_module.APPROVED_LAYOUT_SEGMENT_REGISTRY)
    registry[document.edition_year] = registry[document.edition_year]._replace(
        source_sha256=source_sha256
    )
    monkeypatch.setattr(ocr_module, "APPROVED_LAYOUT_SEGMENT_REGISTRY", registry)
    return document


def _mock_verified_pdf(
    monkeypatch: pytest.MonkeyPatch, document: SourceDocument, pdf: _Pdf
) -> None:
    verified_bytes = b"synthetic verified PDF bytes"

    def verified_source(source_path: Path, *, expected_sha256: str) -> bytes:
        assert type(source_path) is type(Path())
        assert expected_sha256 == document.sha256
        return verified_bytes

    def open_verified(*, stream: bytes, filetype: str) -> _Pdf:
        assert stream == verified_bytes
        assert filetype == "pdf"
        return pdf

    monkeypatch.setattr(
        ocr_module, "_read_verified_apple_vision_source_pdf", verified_source
    )
    monkeypatch.setattr(pymupdf, "open", open_verified)


def test_substituted_same_page_count_pdf_is_rejected_before_pdf_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches manifest SHA being stamped onto an unrelated same-length PDF."""
    approved_source = tmp_path / "approved.pdf"
    substituted_source = tmp_path / "substituted-private.pdf"
    page_count = _document().pdf_page_count
    _write_pdf(approved_source, page_count=page_count, title="approved")
    _write_pdf(substituted_source, page_count=page_count, title="substituted")
    document = _document_with_source_sha256(
        monkeypatch, hashlib.sha256(approved_source.read_bytes()).hexdigest()
    )
    opened = False

    def tracked_open(_: object) -> _Pdf:
        nonlocal opened
        opened = True
        return _Pdf(document.pdf_page_count)

    monkeypatch.setattr(pymupdf, "open", tracked_open)

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            substituted_source,
            document,
            (1,),
            _Adapter(_canonical_bytes(_runtime_payload())),
            _runtime(),
        )

    assert opened is False
    _assert_value_free(
        caught.value, "approved Apple Vision source PDF identity is invalid"
    )


def test_matching_source_is_opened_from_the_exact_verified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a verified path being reopened instead of parsing its checked bytes."""
    source = tmp_path / "approved.pdf"
    page_count = _document().pdf_page_count
    _write_pdf(source, page_count=page_count, title="approved")
    source_bytes = source.read_bytes()
    document = _document_with_source_sha256(
        monkeypatch, hashlib.sha256(source_bytes).hexdigest()
    )
    pdf = _Pdf(page_count)
    opened_stream: bytes | None = None

    def open_verified(*, stream: bytes, filetype: str) -> _Pdf:
        nonlocal opened_stream
        assert filetype == "pdf"
        opened_stream = stream
        return pdf

    monkeypatch.setattr(pymupdf, "open", open_verified)

    records = ocr_module.extract_apple_vision_document(
        source,
        document,
        (1,),
        _Adapter(_canonical_bytes(_runtime_payload())),
        _runtime(),
    )

    assert opened_stream == source_bytes
    assert pdf.closed is True
    assert len(records) == 1


@pytest.mark.parametrize("source_kind", ["symlink", "oversized", "path-subclass"])
def test_untrusted_source_file_is_rejected_before_pdf_open_without_value_leak(
    source_kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches links, unbounded files, or hostile path objects reaching the PDF parser."""
    expected_sha256 = "0" * 64
    source: Path
    if source_kind == "symlink":
        target = tmp_path / "PRIVATE-source-target.pdf"
        target.write_bytes(b"private source")
        expected_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        source = tmp_path / "source.pdf"
        source.symlink_to(target)
    elif source_kind == "oversized":
        source = tmp_path / "PRIVATE-oversized-source.pdf"
        with source.open("wb") as stream:
            stream.truncate(1024 * 1024 * 1024)
    else:
        source = _HostilePath(tmp_path / "PRIVATE-hostile-source.pdf")
    document = _document_with_source_sha256(monkeypatch, expected_sha256)
    opened = False

    def forbidden_open(*args: object, **kwargs: object) -> None:
        nonlocal opened
        del args, kwargs
        opened = True

    monkeypatch.setattr(pymupdf, "open", forbidden_open)

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            source,
            document,
            (1,),
            _Adapter(_canonical_bytes(_runtime_payload())),
            _runtime(),
        )

    assert opened is False
    assert "PRIVATE" not in str(caught.value)
    _assert_value_free(
        caught.value, "approved Apple Vision source PDF identity is invalid"
    )


def test_source_path_swap_during_hashing_is_rejected_before_pdf_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a pathname swap between descriptor verification and PDF parsing."""
    source = tmp_path / "approved.pdf"
    substitute = tmp_path / "PRIVATE-substitute.pdf"
    source.write_bytes(b"approved source bytes")
    substitute.write_bytes(b"substituted source bytes")
    document = _document_with_source_sha256(
        monkeypatch, hashlib.sha256(source.read_bytes()).hexdigest()
    )
    original_read = os.read
    swapped = False
    opened = False

    def swap_path(descriptor: int, byte_count: int) -> bytes:
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            substitute.replace(source)
        return original_read(descriptor, byte_count)

    def forbidden_open(*args: object, **kwargs: object) -> None:
        nonlocal opened
        del args, kwargs
        opened = True

    monkeypatch.setattr(os, "read", swap_path)
    monkeypatch.setattr(pymupdf, "open", forbidden_open)

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            source,
            document,
            (1,),
            _Adapter(_canonical_bytes(_runtime_payload())),
            _runtime(),
        )

    assert swapped is True
    assert opened is False
    _assert_value_free(
        caught.value, "approved Apple Vision source PDF identity is invalid"
    )


def test_document_extraction_emits_direct_runtime_bound_v3_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches Apple Vision output being routed through a synthetic v2 record."""
    document = _document()
    runtime = _runtime()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    pdf = _Pdf(document.pdf_page_count)
    _mock_verified_pdf(monkeypatch, document, pdf)

    records = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (1,),
        _Adapter(runtime_bytes),
        runtime,
    )

    assert pdf.closed is True
    assert len(records) == 1
    record = records[0]
    assert type(record) is ocr_module.ExtractedAppleVisionOcrPageRecord
    assert record.schema_version == 3
    assert record.runtime_provenance == runtime
    assert record.pdf_page_index == 1
    assert record.page_label is None
    assert record.render_dpi == 350
    assert record.raw_page.layout_evidence.status == "not_detected"
    serialized = record.model_dump(mode="json")
    assert "image_digest" not in serialized
    unsigned = {
        key: value for key, value in serialized.items() if key != "fingerprint_sha256"
    }
    assert (
        record.fingerprint_sha256
        == ocr_module.apple_vision_record_fingerprint_sha256(unsigned)
    )


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ((), "approved Apple Vision selected pages are invalid"),
        ((7, 1), "approved Apple Vision selected pages are invalid"),
        ((1, 1), "approved Apple Vision selected pages are invalid"),
        ((True,), "approved Apple Vision selected pages are invalid"),
        ((325,), "approved Apple Vision selected pages are invalid"),
    ],
)
def test_selected_pages_fail_closed_before_pdf_open(
    pages: tuple[object, ...],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches empty, reordered, duplicate, bool, or out-of-range page selection."""
    opened = False

    def forbidden_open(_: object) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            _document(),
            pages,  # type: ignore[arg-type]
            _Adapter(_canonical_bytes(_runtime_payload())),
            _runtime(),
        )

    assert opened is False
    _assert_value_free(caught.value, message)


@pytest.mark.parametrize("mutation", ["wrong-method", "wrong-year", "forged-nested"])
def test_only_recursively_valid_approved_2024_2025_document_reaches_pdf_open(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches method/year drift or nested model_construct bypass before source I/O."""
    document = _document()
    if mutation == "wrong-method":
        candidate = document.model_copy(update={"extraction_method": "native"})
    elif mutation == "wrong-year":
        candidate = _document(2023)
    else:
        profile = document.page_size_profiles[0]
        forged_profile = type(profile).model_construct(
            **{**profile.__dict__, "end_pdf_page": 1}
        )
        candidate = SourceDocument.model_construct(
            **{**document.__dict__, "page_size_profiles": (forged_profile,)}
        )
    opened = False

    def forbidden_open(_: object) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            candidate,
            (1,),
            _Adapter(_canonical_bytes(_runtime_payload())),
            _runtime(),
        )

    assert opened is False
    _assert_value_free(
        caught.value, "approved Apple Vision document contract is invalid"
    )


def test_recursively_invalid_runtime_fails_before_pdf_open_without_value_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a model_construct runtime bypass being attached to page records."""
    marker = "SYNTHETIC_PRIVATE_RUNTIME"
    runtime = _runtime()
    fields = dict(runtime.__dict__)
    fields.pop("sdk_version")
    fields["macos_build"] = marker
    forged = ocr_module.AppleVisionRuntimeProvenance.model_construct(**fields)
    opened = False

    def forbidden_open(_: object) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            _document(),
            (1,),
            _Adapter(_canonical_bytes(_runtime_payload())),
            forged,
        )

    assert opened is False
    assert marker not in str(caught.value)
    _assert_value_free(caught.value, "Apple Vision runtime contract is invalid")


def test_adapter_runtime_must_match_revalidated_run_before_pdf_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches CLI/runtime drift attaching the wrong helper identity to a run."""
    opened = False

    def forbidden_open(_: object) -> None:
        nonlocal opened
        opened = True

    monkeypatch.setattr(pymupdf, "open", forbidden_open)
    different_runtime = _canonical_bytes(_runtime_payload(helper_binary="4" * 64))

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            _document(),
            (1,),
            _Adapter(different_runtime),
            _runtime(),
        )

    assert opened is False
    _assert_value_free(caught.value, "Apple Vision runtime contract is invalid")


def test_stale_extractor_pipeline_replay_is_rejected_before_source_or_pdf_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches matching stale adapter/run bytes replaying an earlier extractor."""
    source_checked = False
    pdf_opened = False
    stale_digest = "f" * 64
    if (
        stale_digest
        == hashlib.sha256(Path(ocr_module.__file__).read_bytes()).hexdigest()
    ):
        stale_digest = "e" * 64
    stale_bytes = _canonical_bytes(_runtime_payload(extractor_pipeline=stale_digest))
    stale_runtime = ocr_module.build_apple_vision_runtime_provenance(stale_bytes)

    def stale_source(*_: object, **__: object) -> None:
        nonlocal source_checked
        source_checked = True

    def forbidden_open(*_: object, **__: object) -> None:
        nonlocal pdf_opened
        pdf_opened = True

    monkeypatch.setattr(
        ocr_module, "_read_verified_apple_vision_source_pdf", stale_source
    )
    monkeypatch.setattr(pymupdf, "open", forbidden_open)

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            _document(),
            (1,),
            _Adapter(stale_bytes),
            stale_runtime,
        )

    assert source_checked is False
    assert pdf_opened is False
    _assert_value_free(caught.value, "Apple Vision runtime contract is invalid")


@pytest.mark.parametrize(
    "invalid_digest",
    [
        pytest.param(True, id="bool"),
        pytest.param(7, id="int"),
        pytest.param(None, id="none"),
        pytest.param("SYNTHETIC_PRIVATE_PIPELINE", id="private-string"),
    ],
)
def test_extractor_pipeline_digest_rejects_wrong_type_or_value_without_leak(
    invalid_digest: object,
) -> None:
    """Catches coerced or diagnostic-leaking extractor identities."""
    raw = _canonical_bytes(_runtime_payload(extractor_pipeline=invalid_digest))

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.build_apple_vision_runtime_provenance(raw)

    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    _assert_value_free(caught.value, "Apple Vision runtime provenance is invalid")


def test_pdf_close_failure_is_fixed_and_context_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches private PDF backend detail surviving as implicit exception context."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    _mock_verified_pdf(
        monkeypatch,
        document,
        _Pdf(document.pdf_page_count, close_failure=True),
    )

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.extract_apple_vision_document(
            Path("private-source-name.pdf"),
            document,
            (1,),
            _Adapter(runtime_bytes),
            _runtime(),
        )

    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    _assert_value_free(caught.value, "cannot close approved Apple Vision source PDF")


def test_expected_adapter_failure_becomes_value_free_runtime_bound_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches helper exception detail escaping or losing v3 runtime provenance."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    _mock_verified_pdf(monkeypatch, document, _Pdf(document.pdf_page_count))

    record = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (1,),
        _FailingAdapter(runtime_bytes),
        _runtime(),
    )[0]

    assert type(record) is ocr_module.QuarantinedAppleVisionOcrPageRecord
    assert record.reason_code == "ocr-adapter-failed"
    assert record.render_sha256 is not None
    assert "SYNTHETIC_PRIVATE" not in str(record.model_dump(mode="json"))
    unsigned = record.model_dump(mode="json", exclude={"fingerprint_sha256"})
    assert (
        record.fingerprint_sha256
        == ocr_module.apple_vision_record_fingerprint_sha256(unsigned)
    )


def test_non_tuple_helper_output_is_quarantined_instead_of_being_coerced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an untrusted helper result bypassing the exact adapter-line boundary."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    _mock_verified_pdf(monkeypatch, document, _Pdf(document.pdf_page_count))

    record = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (1,),
        _ListAdapter(runtime_bytes),  # type: ignore[arg-type]
        _runtime(),
    )[0]

    assert type(record) is ocr_module.QuarantinedAppleVisionOcrPageRecord
    assert record.reason_code == "ocr-provenance-invalid"


def test_writer_is_atomic_deterministic_and_bound_to_independent_runtime_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches stale, path-dependent, reordered, or runtime-rebound v3 JSONL."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    runtime = _runtime()
    _mock_verified_pdf(monkeypatch, document, _Pdf(document.pdf_page_count))
    records = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (1, 7),
        _Adapter(runtime_bytes),
        runtime,
    )
    runtime_fingerprint = "sha256:" + hashlib.sha256(runtime_bytes).hexdigest()
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_text("STALE PRIVATE VALUE\n", encoding="utf-8")

    ocr_module.write_apple_vision_jsonl(
        first,
        records,
        document=document,
        expected_runtime_fingerprint=runtime_fingerprint,
        selected_page_indexes=(1, 7),
    )
    ocr_module.write_apple_vision_jsonl(
        second,
        tuple(reversed(records)),
        document=document,
        expected_runtime_fingerprint=runtime_fingerprint,
        selected_page_indexes=(1, 7),
    )

    assert first.read_bytes() == second.read_bytes()
    assert b"STALE PRIVATE VALUE" not in first.read_bytes()
    payloads = [
        json.loads(line) for line in first.read_text(encoding="utf-8").splitlines()
    ]
    assert [payload["pdf_page_index"] for payload in payloads] == [1, 7]
    assert [payload["page_label"] for payload in payloads] == [None, "7"]
    assert all(payload["schema_version"] == 3 for payload in payloads)
    assert all("image_digest" not in payload for payload in payloads)


def _fingerprint_unsigned_record(payload: dict[str, object]) -> str:
    rendered = _canonical_bytes(payload)
    return hashlib.sha256(
        b"sen-qa-apple-vision-page-record-v3\0" + rendered
    ).hexdigest()


def test_writer_rejects_runtime_rebinding_or_mixed_runtime_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a valid record set being rebound to another independently locked run."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    other_runtime_bytes = _canonical_bytes(_runtime_payload(helper_binary="4" * 64))
    _mock_verified_pdf(monkeypatch, document, _Pdf(document.pdf_page_count))
    first = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (1,),
        _Adapter(runtime_bytes),
        _runtime(),
    )[0]
    second = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (7,),
        _Adapter(other_runtime_bytes),
        _runtime(helper_binary="4" * 64),
    )[0]
    expected = "sha256:" + hashlib.sha256(runtime_bytes).hexdigest()

    with pytest.raises(ocr_module.OcrExtractionError) as mixed:
        ocr_module.write_apple_vision_jsonl(
            tmp_path / "mixed.jsonl",
            (first, second),
            document=document,
            expected_runtime_fingerprint=expected,
            selected_page_indexes=(1, 7),
        )
    with pytest.raises(ocr_module.OcrExtractionError) as rebound:
        ocr_module.write_apple_vision_jsonl(
            tmp_path / "rebound.jsonl",
            (first,),
            document=document,
            expected_runtime_fingerprint="sha256:" + "f" * 64,
            selected_page_indexes=(1,),
        )

    _assert_value_free(
        mixed.value, "Apple Vision page records do not match approved run"
    )
    _assert_value_free(
        rebound.value, "Apple Vision page records do not match approved run"
    )
    assert not (tmp_path / "mixed.jsonl").exists()
    assert not (tmp_path / "rebound.jsonl").exists()


def test_writer_rejects_self_consistent_wrong_page_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a fingerprint-valid record bypassing the manifest page-label policy."""
    document = _document()
    runtime_bytes = _canonical_bytes(_runtime_payload())
    _mock_verified_pdf(monkeypatch, document, _Pdf(document.pdf_page_count))
    record = ocr_module.extract_apple_vision_document(
        Path("approved.pdf"),
        document,
        (7,),
        _Adapter(runtime_bytes),
        _runtime(),
    )[0]
    assert type(record) is ocr_module.ExtractedAppleVisionOcrPageRecord
    unsigned_fields = dict(record.__dict__)
    unsigned_fields.pop("fingerprint_sha256")
    unsigned_fields["page_label"] = "8"
    unsigned_fields["raw_page"] = record.raw_page.model_copy(update={"page_label": "8"})
    unsigned_json = record.model_dump(mode="json", exclude={"fingerprint_sha256"})
    unsigned_json["page_label"] = "8"
    raw_page_json = unsigned_json["raw_page"]
    assert type(raw_page_json) is dict
    raw_page_json["page_label"] = "8"
    forged = ocr_module.ExtractedAppleVisionOcrPageRecord.model_validate(
        {
            **unsigned_fields,
            "fingerprint_sha256": _fingerprint_unsigned_record(unsigned_json),
        }
    )

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.write_apple_vision_jsonl(
            tmp_path / "wrong-label.jsonl",
            (forged,),
            document=document,
            expected_runtime_fingerprint=(
                "sha256:" + hashlib.sha256(runtime_bytes).hexdigest()
            ),
            selected_page_indexes=(7,),
        )

    _assert_value_free(
        caught.value, "Apple Vision page records do not match approved run"
    )
    assert not (tmp_path / "wrong-label.jsonl").exists()
