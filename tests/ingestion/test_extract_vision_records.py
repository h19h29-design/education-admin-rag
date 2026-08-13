"""Strict v3 record contracts for local Apple Vision OCR output."""

from __future__ import annotations

import hashlib
import json
import traceback
import warnings
from pathlib import Path

import pytest
from pydantic import BaseModel

import src.ingestion.extract_ocr as ocr_module
from src.ingestion.extract_common import (
    APPROVED_LAYOUT_DETECTOR_VERSION,
    BoundingBox,
    LayoutEvidence,
    RawBlock,
    RawPage,
)

_SOURCE_2024_SHA256 = "fc1494eff8ee3fe9b53606dd5f55468d8ec254b9d2d661fba6c5e4b46daa99ed"


def _runtime_payload() -> dict[str, object]:
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
        "adapter_sha256": "3" * 64,
        "extractor_pipeline_sha256": hashlib.sha256(
            Path(ocr_module.__file__).read_bytes()
        ).hexdigest(),
        "pymupdf_version": "1.26.7",
    }


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def _json_default(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError


def _record_fingerprint(payload: dict[str, object]) -> str:
    rendered = (
        json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")
    return hashlib.sha256(
        b"sen-qa-apple-vision-page-record-v3\0" + rendered
    ).hexdigest()


def _runtime() -> object:
    return ocr_module.build_apple_vision_runtime_provenance(
        _canonical_bytes(_runtime_payload())
    )


def _raw_page() -> RawPage:
    page_bbox = BoundingBox(x0=0.0, y0=0.0, x1=100.0, y1=200.0)
    return RawPage(
        doc_id="sen-qa-2024",
        edition_year=2024,
        extraction_source="ocr",
        pdf_page_index=1,
        page_label=None,
        page_width=100.0,
        page_height=200.0,
        render_sha256="a" * 64,
        raw_blocks=(RawBlock(bbox=page_bbox, lines=()),),
        layout_evidence=LayoutEvidence(
            status="failed",
            detector_version=APPROVED_LAYOUT_DETECTOR_VERSION,
        ),
    )


def _extracted_payload(runtime: object) -> dict[str, object]:
    return {
        "schema_version": 3,
        "status": "extracted",
        "doc_id": "sen-qa-2024",
        "edition_year": 2024,
        "pdf_page_index": 1,
        "page_label": None,
        "source_sha256": _SOURCE_2024_SHA256,
        "render_sha256": "a" * 64,
        "render_dpi": 350,
        "runtime_provenance": runtime,
        "quality_flags": ("source_150dpi",),
        "raw_page": _raw_page(),
        "layout_segment_provenance": None,
        "review_queue": (),
        "critical_review_policy": "all-fields-human-verification",
        "critical_fields": tuple(
            ocr_module.CriticalFieldStatus(
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


def _quarantined_payload(runtime: object) -> dict[str, object]:
    return {
        "schema_version": 3,
        "status": "quarantined",
        "doc_id": "sen-qa-2024",
        "edition_year": 2024,
        "pdf_page_index": 1,
        "page_label": None,
        "source_sha256": _SOURCE_2024_SHA256,
        "render_sha256": None,
        "render_dpi": 350,
        "runtime_provenance": runtime,
        "quality_flags": ("source_150dpi",),
        "reason_code": "page-render-failed",
    }


def _signed_extracted() -> object:
    payload = _extracted_payload(_runtime())
    return ocr_module.ExtractedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=_record_fingerprint(payload),
    )


def _signed_quarantined() -> object:
    payload = _quarantined_payload(_runtime())
    return ocr_module.QuarantinedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=_record_fingerprint(payload),
    )


def test_runtime_provenance_is_built_only_from_complete_canonical_attestation() -> None:
    """Catches local OCR records omitting the extractor that creates page evidence."""
    raw = _canonical_bytes(_runtime_payload())

    provenance = ocr_module.build_apple_vision_runtime_provenance(raw)

    assert type(provenance) is ocr_module.AppleVisionRuntimeProvenance
    assert provenance.model_dump(mode="json") == _runtime_payload()
    assert (
        provenance.extractor_pipeline_sha256
        == hashlib.sha256(Path(ocr_module.__file__).read_bytes()).hexdigest()
    )
    alternate_payload = _runtime_payload()
    alternate_payload["extractor_pipeline_sha256"] = "f" * 64
    alternate_raw = _canonical_bytes(alternate_payload)
    assert hashlib.sha256(alternate_raw).hexdigest() != hashlib.sha256(raw).hexdigest()


def test_incomplete_v1_runtime_provenance_is_rejected_value_free() -> None:
    """Catches pre-pipeline attestations being replayed as complete provenance."""
    payload = _runtime_payload()
    payload["schema_version"] = "sen-qa-apple-vision-runtime-provenance/v1"
    payload.pop("extractor_pipeline_sha256")

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.build_apple_vision_runtime_provenance(_canonical_bytes(payload))

    assert str(caught.value) == "Apple Vision runtime provenance is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_extracted_v3_record_replaces_image_digest_with_bound_runtime_provenance() -> (
    None
):
    """Catches a local Vision page inheriting the unrelated v2 container identity."""
    payload = _extracted_payload(_runtime())
    fingerprint = _record_fingerprint(payload)

    record = ocr_module.ExtractedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=fingerprint,
    )

    serialized = record.model_dump(mode="json")
    assert serialized["schema_version"] == 3
    assert serialized["runtime_provenance"] == _runtime_payload()
    assert serialized["fingerprint_sha256"] == fingerprint
    assert "image_digest" not in serialized


def test_quarantined_v3_record_is_value_free_and_bound_to_the_same_runtime() -> None:
    """Catches a failed local page losing runtime identity or retaining image fields."""
    payload = _quarantined_payload(_runtime())
    fingerprint = _record_fingerprint(payload)

    record = ocr_module.QuarantinedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=fingerprint,
    )

    serialized = record.model_dump(mode="json")
    assert set(serialized) == {
        "schema_version",
        "status",
        "doc_id",
        "edition_year",
        "pdf_page_index",
        "page_label",
        "source_sha256",
        "render_sha256",
        "render_dpi",
        "runtime_provenance",
        "quality_flags",
        "reason_code",
        "fingerprint_sha256",
    }
    assert serialized["runtime_provenance"] == _runtime_payload()


def test_v3_record_validator_recursively_rebuilds_the_exact_record_type() -> None:
    """Catches a boundary trusting an already-constructed nested Pydantic object."""
    payload = _extracted_payload(_runtime())
    record = ocr_module.ExtractedAppleVisionOcrPageRecord(
        **payload,
        fingerprint_sha256=_record_fingerprint(payload),
    )

    validated = ocr_module.validate_apple_vision_ocr_page_record(record)

    assert type(validated) is ocr_module.ExtractedAppleVisionOcrPageRecord
    assert validated == record
    assert validated is not record
    assert validated.runtime_provenance is not record.runtime_provenance
    assert validated.raw_page is not record.raw_page


@pytest.mark.parametrize("as_bytes", [False, True])
@pytest.mark.parametrize(
    ("record", "expected_type"),
    [
        pytest.param(
            _signed_extracted,
            lambda: ocr_module.ExtractedAppleVisionOcrPageRecord,
            id="v3-extracted",
        ),
        pytest.param(
            _signed_quarantined,
            lambda: ocr_module.QuarantinedAppleVisionOcrPageRecord,
            id="v3-quarantined",
        ),
        pytest.param(
            lambda: ocr_module.QuarantinedOcrPageRecord(
                schema_version=2,
                doc_id="fixture-2025",
                edition_year=2025,
                pdf_page_index=1,
                page_label="1",
                source_sha256="c" * 64,
                render_sha256=None,
                render_dpi=300,
                image_digest="sha256:" + "d" * 64,
                quality_flags=(),
                reason_code="page-render-failed",
            ),
            lambda: ocr_module.QuarantinedOcrPageRecord,
            id="v2-quarantined",
        ),
    ],
)
def test_public_parser_dispatches_exact_schema_and_status_for_bytes_or_dict(
    record: object,
    expected_type: object,
    as_bytes: bool,
) -> None:
    """Catches v3 records being guessed as v2 or extracted/quarantine ambiguity."""
    instance = record()
    payload = instance.model_dump(mode="json")
    value: bytes | dict[str, object] = (
        _canonical_bytes(payload) if as_bytes else payload
    )

    parsed = ocr_module.parse_ocr_page_record(value)

    assert type(parsed) is expected_type()


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            lambda payload: payload.__setitem__("schema_version", 2),
            id="schema-downgrade",
        ),
        pytest.param(
            lambda payload: payload.pop("runtime_provenance"),
            id="missing-runtime",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("image_digest", "sha256:" + "4" * 64),
            id="v2-image-digest-extra",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("fingerprint_sha256", "f" * 64),
            id="forged-fingerprint",
        ),
        pytest.param(
            lambda payload: payload.__setitem__("status", "failed"),
            id="unknown-status",
        ),
    ],
)
def test_parser_rejects_downgraded_incomplete_extra_or_forged_v3_record(
    mutation: object,
) -> None:
    """Catches schema confusion or a partial runtime binding entering review input."""
    payload = _signed_extracted().model_dump(mode="json")
    mutation(payload)

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.parse_ocr_page_record(payload)

    assert str(caught.value) == "OCR page record is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("mutation", ["missing", "extra", "noncanonical"])
def test_runtime_attestation_rejects_missing_extra_or_noncanonical_fields(
    mutation: str,
) -> None:
    """Catches a runtime identity assembled from optional or ambiguous metadata."""
    payload = _runtime_payload()
    if mutation == "missing":
        payload.pop("sdk_version")
        raw = _canonical_bytes(payload)
    elif mutation == "extra":
        payload["private_runtime_note"] = "must-not-be-retained"
        raw = _canonical_bytes(payload)
    else:
        raw = json.dumps(payload, sort_keys=True).encode("ascii")

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.build_apple_vision_runtime_provenance(raw)

    assert str(caught.value) == "Apple Vision runtime provenance is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_recursive_validator_rejects_constructed_runtime_without_value_leak() -> None:
    """Catches model_construct bypasses inside the required runtime provenance."""
    marker = "SYNTHETIC_PRIVATE_VISION_RUNTIME"
    record = _signed_extracted()
    runtime_fields = dict(record.runtime_provenance.__dict__)
    runtime_fields.pop("extractor_pipeline_sha256")
    runtime_fields["macos_build"] = marker
    forged_runtime = ocr_module.AppleVisionRuntimeProvenance.model_construct(
        **runtime_fields
    )
    forged_record = ocr_module.ExtractedAppleVisionOcrPageRecord.model_construct(
        **{**record.__dict__, "runtime_provenance": forged_runtime}
    )

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        with pytest.raises(ocr_module.OcrExtractionError) as caught:
            ocr_module.validate_apple_vision_ocr_page_record(forged_record)

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
            *(str(item.message) for item in caught_warnings),
        )
    )
    assert marker not in diagnostics
    assert str(caught.value) == "Apple Vision page record is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert caught_warnings == []


def test_parser_failure_is_value_free_even_when_private_fields_are_malformed() -> None:
    """Catches recognized or operator-provided values leaking through parse errors."""
    marker = "SYNTHETIC_PRIVATE_VISION_RECORD"
    payload = _signed_quarantined().model_dump(mode="json")
    payload["doc_id"] = marker

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.parse_ocr_page_record(payload)

    diagnostics = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(traceback.format_exception(caught.value)),
        )
    )
    assert marker not in diagnostics
    assert str(caught.value) == "OCR page record is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("status", ["extracted", "quarantined"])
def test_v3_parser_rejects_self_consistent_2023_year_replay(status: str) -> None:
    """Catches a local Vision record replayed into the preserved Paddle-only year."""
    if status == "extracted":
        payload = _extracted_payload(_runtime())
        raw_page = payload["raw_page"]
        assert isinstance(raw_page, RawPage)
        payload["raw_page"] = raw_page.model_copy(
            update={
                "edition_year": 2023,
                "layout_evidence": LayoutEvidence(),
            }
        )
    else:
        payload = _quarantined_payload(_runtime())
    payload["edition_year"] = 2023
    payload["render_dpi"] = 300
    payload["quality_flags"] = ("source_approx_96dpi",)
    payload["fingerprint_sha256"] = _record_fingerprint(payload)
    raw = (
        json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")

    with pytest.raises(ocr_module.OcrExtractionError) as caught:
        ocr_module.parse_ocr_page_record(raw)

    assert str(caught.value) == "OCR page record is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
