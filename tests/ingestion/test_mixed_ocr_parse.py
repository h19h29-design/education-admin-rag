"""Mixed OCR parser boundaries with externally pinned execution authority."""

from __future__ import annotations

import hashlib
import json
import traceback
from pathlib import Path
from typing import Any, cast

import pytest

import src.ingestion.extract_ocr as ocr_module
from src.ingestion.extract_common import (
    APPROVED_LAYOUT_DETECTOR_VERSION,
    BoundingBox,
    LayoutEvidence,
    RawBlock,
    RawPage,
)
from src.ingestion.extract_ocr import (
    AppleVisionRuntimeProvenance,
    CriticalFieldStatus,
    ExtractedAppleVisionOcrPageRecord,
    ExtractedOcrPageRecord,
    QuarantinedAppleVisionOcrPageRecord,
    QuarantinedOcrPageRecord,
    apple_vision_record_fingerprint_sha256,
    build_apple_vision_runtime_provenance,
)
from src.ingestion.manifest import SourceDocument, load_manifest, page_label
from src.ingestion.ocr_authority import (
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
)
from src.ingestion.parse_common import parser_page_from_ocr_record
from src.ingestion.parse_metadata import ParseMetadataError, build_parse_metadata

_MANIFEST_PATH = Path("data/manifests/sen_qa_sources.json")
_PADDLE_DIGEST = (
    "sha256:1b13f568237b23bbe858bef1bac1ef7081094554f3d3ba5750c4dae72feec9d6"
)


def _canonical_json(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _runtime_payload(*, adapter_sha256: str = "3" * 64) -> dict[str, object]:
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
        "helper_binary_sha256": "2" * 64,
        "adapter_sha256": adapter_sha256,
        "extractor_pipeline_sha256": hashlib.sha256(
            Path(ocr_module.__file__).read_bytes()
        ).hexdigest(),
        "pymupdf_version": "1.26.7",
    }


def _runtime(*, adapter_sha256: str = "3" * 64) -> AppleVisionRuntimeProvenance:
    return build_apple_vision_runtime_provenance(
        _canonical_json(_runtime_payload(adapter_sha256=adapter_sha256))
    )


def _runtime_fingerprint(runtime: AppleVisionRuntimeProvenance) -> str:
    raw = _canonical_json(runtime.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _documents() -> dict[int, SourceDocument]:
    return {
        document.edition_year: document for document in load_manifest(_MANIFEST_PATH)
    }


def _label(document: SourceDocument, index: int) -> str | None:
    value = page_label(document.page_numbering, index)
    return str(value) if value is not None else None


def _vision_extracted(
    document: SourceDocument,
    runtime: AppleVisionRuntimeProvenance,
    *,
    page_index: int = 1,
) -> ExtractedAppleVisionOcrPageRecord:
    assert document.render_dpi is not None
    profile = document.page_size_profiles[0]
    render_sha256 = "a" * 64
    raw_page = RawPage(
        doc_id=document.doc_id,
        edition_year=cast(Any, document.edition_year),
        extraction_source="ocr",
        pdf_page_index=page_index,
        page_label=_label(document, page_index),
        page_width=profile.width_pt,
        page_height=profile.height_pt,
        render_sha256=render_sha256,
        raw_blocks=(
            RawBlock(
                bbox=BoundingBox(
                    x0=0.0,
                    y0=0.0,
                    x1=profile.width_pt,
                    y1=profile.height_pt,
                ),
                lines=(),
            ),
        ),
        layout_evidence=LayoutEvidence(
            status="failed",
            detector_version=APPROVED_LAYOUT_DETECTOR_VERSION,
        ),
    )
    payload: dict[str, object] = {
        "schema_version": 3,
        "status": "extracted",
        "doc_id": document.doc_id,
        "edition_year": document.edition_year,
        "pdf_page_index": page_index,
        "page_label": _label(document, page_index),
        "source_sha256": document.sha256,
        "render_sha256": render_sha256,
        "render_dpi": document.render_dpi,
        "runtime_provenance": runtime,
        "quality_flags": ("source_150dpi",),
        "raw_page": raw_page,
        "layout_segment_provenance": None,
        "review_queue": (),
        "critical_review_policy": "all-fields-human-verification",
        "critical_fields": tuple(
            CriticalFieldStatus(
                field_type=field_type,
                status="unverified",
                review_required=True,
            )
            for field_type in (
                "title",
                "question",
                "amount",
                "date",
                "law_name",
                "article",
            )
        ),
        "review_status": "needs_review",
        "search_eligible": False,
        "answer_eligible": False,
    }
    return ExtractedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=apple_vision_record_fingerprint_sha256(payload),
    )


def _vision_quarantine(
    document: SourceDocument,
    runtime: AppleVisionRuntimeProvenance,
    *,
    page_index: int = 1,
) -> QuarantinedAppleVisionOcrPageRecord:
    assert document.render_dpi is not None
    payload: dict[str, object] = {
        "schema_version": 3,
        "status": "quarantined",
        "doc_id": document.doc_id,
        "edition_year": document.edition_year,
        "pdf_page_index": page_index,
        "page_label": _label(document, page_index),
        "source_sha256": document.sha256,
        "render_sha256": None,
        "render_dpi": document.render_dpi,
        "runtime_provenance": runtime,
        "quality_flags": ("source_150dpi",),
        "reason_code": "page-render-failed",
    }
    return QuarantinedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=apple_vision_record_fingerprint_sha256(payload),
    )


def _v2_quarantine(
    document: SourceDocument,
    *,
    page_index: int = 1,
    image_digest: str = _PADDLE_DIGEST,
) -> QuarantinedOcrPageRecord:
    assert document.render_dpi is not None
    quality_flags = (
        ("source_approx_96dpi",)
        if document.edition_year == 2023
        else ("source_150dpi",)
    )
    return QuarantinedOcrPageRecord(
        schema_version=2,
        doc_id=document.doc_id,
        edition_year=document.edition_year,
        pdf_page_index=page_index,
        page_label=_label(document, page_index),
        source_sha256=document.sha256,
        render_sha256=None,
        render_dpi=document.render_dpi,
        image_digest=image_digest,
        quality_flags=quality_flags,
        reason_code="page-render-failed",
    )


def _write_jsonl(path: Path, records: tuple[object, ...]) -> None:
    path.write_bytes(
        b"".join(
            _canonical_json(record.model_dump(mode="json"))  # type: ignore[attr-defined]
            for record in records
        )
    )


def _write_authority(
    tmp_path: Path,
    *,
    runtime_2024: AppleVisionRuntimeProvenance,
    runtime_2025: AppleVisionRuntimeProvenance | None = None,
) -> tuple[Path, str]:
    second = runtime_2025 if runtime_2025 is not None else runtime_2024
    lock = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint=_runtime_fingerprint(runtime_2024),
        vision_2025_runtime_fingerprint=_runtime_fingerprint(second),
    )
    raw = canonical_ocr_authority_bytes(lock)
    path = tmp_path / "ocr-authority.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def _authority_call(
    input_path: Path,
    *,
    year: int,
    pages: str,
    lock_path: object,
    lock_sha256: object,
    expected_image_digest: str | None = None,
) -> object:
    return build_parse_metadata(
        input_path,
        manifest_path=_MANIFEST_PATH,
        edition_year=year,
        pages=pages,
        expected_image_digest=expected_image_digest,
        ocr_authority_lock_path=cast(Any, lock_path),
        expected_ocr_authority_lock_sha256=cast(Any, lock_sha256),
    )


def test_v3_extracted_and_quarantined_records_adapt_to_the_same_semantics_as_v2() -> (
    None
):
    """Catches the parser requiring a v2 image digest or dropping v3 review state."""
    document = _documents()[2024]
    runtime = _runtime()
    v3_extracted = _vision_extracted(document, runtime)
    v3_quarantined = _vision_quarantine(document, runtime)
    v2_extracted_payload = v3_extracted.model_dump(
        exclude={"fingerprint_sha256", "runtime_provenance"}
    )
    v2_extracted_payload["schema_version"] = 2
    v2_extracted_payload["image_digest"] = _PADDLE_DIGEST
    v2_extracted = ExtractedOcrPageRecord(**v2_extracted_payload)
    v2_quarantined = _v2_quarantine(document)

    for v2, v3 in (
        (v2_extracted, v3_extracted),
        (v2_quarantined, v3_quarantined),
    ):
        legacy_page = parser_page_from_ocr_record(v2)
        vision_page = parser_page_from_ocr_record(v3)
        assert vision_page == legacy_page
        assert vision_page.upstream_review_status == "needs_review"
        assert "image_digest" not in vision_page.model_dump(mode="json")


def test_v2_2023_authority_uses_the_locked_container_digest(tmp_path: Path) -> None:
    """Catches accepting a v2 record whose image digest is not the lock authority."""
    document = _documents()[2023]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    input_path = tmp_path / "2023.jsonl"
    _write_jsonl(input_path, (_v2_quarantine(document),))

    metadata = _authority_call(
        input_path,
        year=2023,
        pages="1",
        lock_path=lock_path,
        lock_sha256=lock_sha256,
    )

    assert metadata.record_counts == {  # type: ignore[attr-defined]
        "extracted": 0,
        "quarantined": 1,
        "total": 1,
    }

    _write_jsonl(
        input_path,
        (_v2_quarantine(document, image_digest="sha256:" + "d" * 64),),
    )
    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2023,
            pages="1",
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )
    assert caught.value.code == "policy_mismatch"


def test_v3_authority_recomputes_the_canonical_runtime_fingerprint(
    tmp_path: Path,
) -> None:
    """Catches trusting an embedded runtime without matching its canonical hash."""
    document = _documents()[2024]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    input_path = tmp_path / "2024.jsonl"
    _write_jsonl(input_path, (_vision_quarantine(document, runtime),))

    metadata = _authority_call(
        input_path,
        year=2024,
        pages="1",
        lock_path=lock_path,
        lock_sha256=lock_sha256,
    )

    assert metadata.edition_year == 2024  # type: ignore[attr-defined]
    assert metadata.review_counts["upstream_needs_review"] == 0  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("missing-path", "authority_invalid"),
        ("missing-sha", "authority_invalid"),
        ("wrong-sha", "authority_invalid"),
        ("constructed-path", "authority_invalid"),
        ("legacy-and-authority", "authority_invalid"),
        ("runtime-replay", "policy_mismatch"),
    ],
)
def test_authority_is_external_unambiguous_and_not_replayable(
    tmp_path: Path,
    case: str,
    expected_code: str,
) -> None:
    """Catches missing, forged, ambiguous, or another-run authority acceptance."""
    document = _documents()[2024]
    runtime = _runtime()
    other_runtime = _runtime(adapter_sha256="4" * 64)
    lock_path, lock_sha256 = _write_authority(
        tmp_path,
        runtime_2024=other_runtime if case == "runtime-replay" else runtime,
    )
    input_path = tmp_path / "2024.jsonl"
    _write_jsonl(input_path, (_vision_quarantine(document, runtime),))
    selected_path: object = lock_path
    selected_sha: object = lock_sha256
    legacy_digest = None
    if case == "missing-path":
        selected_path = None
    elif case == "missing-sha":
        selected_sha = None
    elif case == "wrong-sha":
        selected_sha = "f" * 64
    elif case == "constructed-path":
        selected_path = object()
    elif case == "legacy-and-authority":
        legacy_digest = _PADDLE_DIGEST

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2024,
            pages="1",
            lock_path=selected_path,
            lock_sha256=selected_sha,
            expected_image_digest=legacy_digest,
        )

    assert caught.value.code == expected_code
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_one_document_cannot_mix_v2_and_v3_records(tmp_path: Path) -> None:
    """Catches record-by-record dispatch concealing a mixed-schema document."""
    document = _documents()[2024]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    input_path = tmp_path / "mixed.jsonl"
    _write_jsonl(
        input_path,
        (
            _vision_quarantine(document, runtime, page_index=1),
            _v2_quarantine(document, page_index=2),
        ),
    )

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2024,
            pages="1-2",
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )

    assert caught.value.code == "input_invalid"


def test_authority_path_rejects_a_v2_downgrade_for_2024(tmp_path: Path) -> None:
    """Catches the legacy schema replacing the lock-selected Vision schema."""
    document = _documents()[2024]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    input_path = tmp_path / "downgrade.jsonl"
    _write_jsonl(input_path, (_v2_quarantine(document),))

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2024,
            pages="1",
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )

    assert caught.value.code == "policy_mismatch"


def test_v3_never_accepts_the_legacy_image_digest_api(tmp_path: Path) -> None:
    """Catches v3 bypassing runtime authority through the legacy v2 argument."""
    document = _documents()[2024]
    runtime = _runtime()
    input_path = tmp_path / "legacy-v3.jsonl"
    _write_jsonl(input_path, (_vision_quarantine(document, runtime),))

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2024,
            pages="1",
            lock_path=None,
            lock_sha256=None,
            expected_image_digest=_PADDLE_DIGEST,
        )

    assert caught.value.code == "image_digest_invalid"


def test_native_input_rejects_unused_ocr_authority(tmp_path: Path) -> None:
    """Catches authority arguments being silently ignored outside OCR parsing."""
    document = _documents()[2020]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    input_path = tmp_path / "native.jsonl"
    record = {
        "schema_version": 2,
        "status": "quarantined",
        "doc_id": document.doc_id,
        "edition_year": 2020,
        "source_sha256": document.sha256,
        "document_pdf_page_count": document.pdf_page_count,
        "pdf_page_index": 1,
        "page_label": _label(document, 1),
        "reason_code": "page-extraction-failed",
    }
    input_path.write_bytes(_canonical_json(record))

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2020,
            pages="all",
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )

    assert caught.value.code == "authority_invalid"


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda payload: payload.__setitem__("schema_version", True), id="type"
        ),
        pytest.param(
            lambda payload: payload["runtime_provenance"].__setitem__(  # type: ignore[union-attr]
                "macos_build", "PRIVATE-RUNTIME-SENTINEL"
            ),
            id="value",
        ),
    ],
)
def test_malformed_v3_jsonl_fails_without_private_value_disclosure(
    tmp_path: Path,
    mutation: Any,
) -> None:
    """Catches type bombs or private runtime values escaping the fixed error."""
    document = _documents()[2024]
    runtime = _runtime()
    lock_path, lock_sha256 = _write_authority(tmp_path, runtime_2024=runtime)
    payload = _vision_quarantine(document, runtime).model_dump(mode="json")
    mutation(payload)
    input_path = tmp_path / "PRIVATE-INPUT-SENTINEL.jsonl"
    input_path.write_bytes(_canonical_json(payload))

    with pytest.raises(ParseMetadataError) as caught:
        _authority_call(
            input_path,
            year=2024,
            pages="1",
            lock_path=lock_path,
            lock_sha256=lock_sha256,
        )

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert caught.value.code == "input_invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "PRIVATE-RUNTIME-SENTINEL" not in diagnostics
    assert "PRIVATE-INPUT-SENTINEL" not in diagnostics
