"""Strict trust-boundary tests for the per-year OCR authority lock."""

from __future__ import annotations

import hashlib
import json
import os
import traceback
from pathlib import Path, PosixPath
from typing import Any, cast

import pytest

from src.ingestion.ocr_authority import (
    OcrAuthorityLockError,
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
    load_ocr_authority_lock,
)

_VISION_2024 = "sha256:" + "24" * 32
_VISION_2025 = "sha256:" + "25" * 32
_FROZEN_SELF_SHA256 = "e3e319f3cdba837fda8f987e1d2a672a706e736ecfa00539a3e87c80b5abc6a6"
_FROZEN_FILE_SHA256 = "28ea740ca2ad5fab63ae4bc5a06d2bd3af77602118221a30b6fa3bfcd367380e"
_FROZEN_CANONICAL_BYTES = (
    b'{"entries":[{"authority":"ghcr.io/h19h29-design/'
    b"education-admin-rag-ingestion@sha256:"
    b'1b13f568237b23bbe858bef1bac1ef7081094554f3d3ba5750c4dae72feec9d6",'
    b'"doc_id":"sen-qa-2023","engine":"paddleocr",'
    b'"record_schema":"sen-qa-ocr-page/v2",'
    b'"source_sha256":"9a6a5b3745eb4200c70f9d33395c8b25b5a55fa171036127f2be5791224455bc",'
    b'"year":2023},{"authority":"sha256:'
    b'2424242424242424242424242424242424242424242424242424242424242424",'
    b'"doc_id":"sen-qa-2024","engine":"apple-vision",'
    b'"record_schema":"sen-qa-ocr-page/v3",'
    b'"source_sha256":"fc1494eff8ee3fe9b53606dd5f55468d8ec254b9d2d661fba6c5e4b46daa99ed",'
    b'"year":2024},{"authority":"sha256:'
    b'2525252525252525252525252525252525252525252525252525252525252525",'
    b'"doc_id":"sen-qa-2025","engine":"apple-vision",'
    b'"record_schema":"sen-qa-ocr-page/v3",'
    b'"source_sha256":"9a1a7b0ebf1346b540c97d9990dd3b43c647ce397322ff0fabe6d2de84c0ce03",'
    b'"year":2025}],"schema_version":"sen-qa-ocr-authority-lock/v1",'
    b'"self_sha256":"e3e319f3cdba837fda8f987e1d2a672a706e736ecfa00539a3e87c80b5abc6a6"}\n'
)


class _HostilePath(PosixPath):
    def __fspath__(self) -> str:
        raise RuntimeError("PRIVATE_PATH_BOMB")


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


def _valid_payload() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_FROZEN_CANONICAL_BYTES))


def _reseal(payload: dict[str, Any]) -> tuple[bytes, str]:
    body = {
        "entries": payload["entries"],
        "schema_version": payload["schema_version"],
    }
    payload["self_sha256"] = hashlib.sha256(_canonical_json(body)).hexdigest()
    raw = _canonical_json(payload)
    return raw, hashlib.sha256(raw).hexdigest()


def _write_lock(tmp_path: Path, raw: bytes = _FROZEN_CANONICAL_BYTES) -> Path:
    path = tmp_path / "ocr-authority.json"
    path.write_bytes(raw)
    return path


def _assert_sanitized_error(call: Any, private: str) -> None:
    with pytest.raises(OcrAuthorityLockError) as caught:
        call()
    rendered = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(
                traceback.format_exception(
                    type(caught.value), caught.value, caught.value.__traceback__
                )
            ),
        )
    )
    assert private not in rendered
    assert str(caught.value) == "OCR authority lock is invalid"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_builder_emits_the_frozen_three_year_canonical_lock() -> None:
    lock = build_ocr_authority_lock(
        vision_2024_runtime_fingerprint=_VISION_2024,
        vision_2025_runtime_fingerprint=_VISION_2025,
    )

    raw = canonical_ocr_authority_bytes(lock)

    assert raw == _FROZEN_CANONICAL_BYTES
    assert lock.self_sha256 == _FROZEN_SELF_SHA256
    assert hashlib.sha256(raw).hexdigest() == _FROZEN_FILE_SHA256
    assert tuple(entry.year for entry in lock.entries) == (2023, 2024, 2025)
    assert tuple(entry.record_schema for entry in lock.entries) == (
        "sen-qa-ocr-page/v2",
        "sen-qa-ocr-page/v3",
        "sen-qa-ocr-page/v3",
    )
    assert tuple(entry.engine for entry in lock.entries) == (
        "paddleocr",
        "apple-vision",
        "apple-vision",
    )


def test_loader_requires_the_independently_supplied_full_file_sha256(
    tmp_path: Path,
) -> None:
    path = _write_lock(tmp_path)

    lock = load_ocr_authority_lock(path, expected_sha256=_FROZEN_FILE_SHA256)

    assert canonical_ocr_authority_bytes(lock) == _FROZEN_CANONICAL_BYTES


def test_loader_rejects_a_correctly_self_resealed_file_under_the_old_external_sha(
    tmp_path: Path,
) -> None:
    payload = _valid_payload()
    payload["entries"][1]["authority"] = "sha256:" + "42" * 32
    raw, _ = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=_FROZEN_FILE_SHA256)


def test_loader_rejects_a_forged_self_fingerprint_even_when_file_sha_matches(
    tmp_path: Path,
) -> None:
    payload = _valid_payload()
    payload["self_sha256"] = "f" * 64
    raw = _canonical_json(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


@pytest.mark.parametrize("entry_count", [2, 4])
def test_loader_rejects_missing_or_extra_year_entries(
    tmp_path: Path, entry_count: int
) -> None:
    payload = _valid_payload()
    if entry_count == 2:
        payload["entries"] = payload["entries"][:2]
    else:
        payload["entries"].append(dict(payload["entries"][2]))
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


def test_loader_rejects_a_duplicate_year(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["entries"][2]["year"] = 2024
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


def test_loader_rejects_a_replayed_year_entry(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["entries"][2] = dict(payload["entries"][1])
    payload["entries"][2]["year"] = 2025
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


def test_loader_rejects_a_wrong_source_binding(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["entries"][2]["source_sha256"] = "0" * 64
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


@pytest.mark.parametrize(
    ("year", "field", "value"),
    [
        (2023, "record_schema", "sen-qa-ocr-page/v3"),
        (2023, "engine", "apple-vision"),
        (2023, "authority", "sha256:" + "23" * 32),
        (2024, "record_schema", "sen-qa-ocr-page/v2"),
        (2024, "engine", "paddleocr"),
        (2025, "authority", "not-a-runtime-fingerprint"),
    ],
)
def test_loader_rejects_wrong_schema_engine_or_authority_kind(
    tmp_path: Path, year: int, field: str, value: object
) -> None:
    payload = _valid_payload()
    entry = next(item for item in payload["entries"] if item["year"] == year)
    entry[field] = value
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"entries": {}}),
        lambda payload: payload["entries"][0].update({"year": True}),
        lambda payload: payload["entries"][0].update({"year": "2023"}),
        lambda payload: payload["entries"][1].update({"authority": False}),
        lambda payload: payload["entries"][2].update({"source_sha256": 25}),
        lambda payload: payload.update({"unexpected": None}),
        lambda payload: payload["entries"][0].update({"unexpected": None}),
    ],
)
def test_loader_rejects_unsafe_types_and_undeclared_fields(
    tmp_path: Path, mutate: Any
) -> None:
    payload = _valid_payload()
    mutate(payload)
    raw, expected = _reseal(payload)
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(path, expected_sha256=expected)


def test_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    private = "PRIVATE_DUPLICATE_SENTINEL"
    raw = _FROZEN_CANONICAL_BYTES.replace(
        b'{"entries":',
        ('{"schema_version":"' + private + '","entries":').encode(),
        1,
    )
    path = _write_lock(tmp_path, raw)

    _assert_sanitized_error(
        lambda: load_ocr_authority_lock(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        ),
        private,
    )


@pytest.mark.parametrize(
    "mutated",
    [
        _FROZEN_CANONICAL_BYTES[:-1],
        b" " + _FROZEN_CANONICAL_BYTES,
        _FROZEN_CANONICAL_BYTES + b"\n",
    ],
)
def test_loader_rejects_noncanonical_serialization(
    tmp_path: Path, mutated: bytes
) -> None:
    path = _write_lock(tmp_path, mutated)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(
            path,
            expected_sha256=hashlib.sha256(mutated).hexdigest(),
        )


def test_loader_rejects_symlink_and_non_regular_files(tmp_path: Path) -> None:
    target = _write_lock(tmp_path)
    link = tmp_path / "authority-link.json"
    os.symlink(target, link)

    for path in (link, tmp_path):
        with pytest.raises(OcrAuthorityLockError):
            load_ocr_authority_lock(path, expected_sha256=_FROZEN_FILE_SHA256)


@pytest.mark.parametrize("raw", [b"", b"x" * 65_537])
def test_loader_rejects_empty_or_oversized_files(tmp_path: Path, raw: bytes) -> None:
    path = _write_lock(tmp_path, raw)

    with pytest.raises(OcrAuthorityLockError):
        load_ocr_authority_lock(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("not-a-path", _FROZEN_FILE_SHA256),
        (Path("unused"), b"not-a-string"),
        (Path("unused"), "F" * 64),
        (Path("unused"), "0" * 63),
    ],
)
def test_loader_rejects_unsafe_arguments_without_value_disclosure(
    path: object, expected: object
) -> None:
    private = str(path)

    _assert_sanitized_error(
        lambda: load_ocr_authority_lock(
            cast(Any, path),
            expected_sha256=cast(Any, expected),
        ),
        private,
    )


def test_loader_rejects_path_subclass_before_fspath_can_run() -> None:
    path = _HostilePath("unused")

    _assert_sanitized_error(
        lambda: load_ocr_authority_lock(
            path,
            expected_sha256=_FROZEN_FILE_SHA256,
        ),
        "PRIVATE_PATH_BOMB",
    )


def test_builder_rejects_unsafe_runtime_fingerprint_without_value_disclosure() -> None:
    private = "PRIVATE_RUNTIME_FINGERPRINT"

    _assert_sanitized_error(
        lambda: build_ocr_authority_lock(
            vision_2024_runtime_fingerprint=private,
            vision_2025_runtime_fingerprint=_VISION_2025,
        ),
        private,
    )
