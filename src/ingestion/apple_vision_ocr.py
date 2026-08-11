"""Fail-closed local Apple Vision adapter for complete RGB page rasters."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import selectors
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Final, NoReturn, Self

from src.ingestion.extract_ocr import (
    AdapterLine,
    OcrAdapterError,
    OcrExtractionError,
    RasterImage,
    _infer_field_type,
    build_apple_vision_runtime_provenance,
)

_SHA256_LENGTH: Final = 64
_MAX_HELPER_BYTES: Final = 32 * 1024 * 1024
_MAX_RESULT_BYTES: Final = 16 * 1024 * 1024
_MAX_LINES: Final = 10_000
_RUNTIME_SCHEMA: Final = "sen-qa-apple-vision-runtime/v1"
_LINES_SCHEMA: Final = "sen-qa-apple-vision-lines/v1"


def _raise_adapter_error(message: str) -> NoReturn:
    raise OcrAdapterError(message)


def _read_verified_helper(path: Path, expected_sha256: str) -> bytes | None:
    if (
        type(path) is not type(Path())
        or type(expected_sha256) is not str
        or len(expected_sha256) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_HELPER_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_HELPER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(raw) > _MAX_HELPER_BYTES
            or identity_before != identity_after
            or hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_identity_file(path: Path, *, max_bytes: int) -> bytes | None:
    if type(path) is not type(Path()) or type(max_bytes) is not int or max_bytes <= 0:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            return None
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > max_bytes or (
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
            return None
        return raw
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_executable(directory: Path, raw: bytes) -> Path | None:
    path = directory / "apple-vision-helper"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o700)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o700)
        return path
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run_helper(
    executable: Path,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes = b"",
) -> bytes | None:
    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryFile(mode="w+b") as input_stream:
            input_stream.write(input_bytes)
            input_stream.flush()
            input_stream.seek(0)
            process = subprocess.Popen(
                (str(executable), *arguments),
                stdin=input_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            stdout = process.stdout
            if stdout is None:
                return None
            deadline = time.monotonic() + 120.0
            result = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(stdout, selectors.EVENT_READ)
                while True:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0.0:
                        return None
                    if not selector.select(remaining_seconds):
                        return None
                    read_size = min(
                        64 * 1024,
                        _MAX_RESULT_BYTES + 1 - len(result),
                    )
                    chunk = os.read(stdout.fileno(), read_size)
                    if not chunk:
                        break
                    result.extend(chunk)
                    if len(result) > _MAX_RESULT_BYTES:
                        return None
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds <= 0.0:
                return None
            if process.wait(timeout=remaining_seconds) != 0:
                return None
            return bytes(result)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                        process.wait(timeout=1)
                    except (OSError, subprocess.SubprocessError):
                        pass
                except OSError:
                    pass
            if process.stdout is not None:
                process.stdout.close()


def _json_object(raw: bytes) -> dict[str, Any] | None:
    if not raw or len(raw) > _MAX_RESULT_BYTES:
        return None
    payload: object | None = None
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        payload = None
    return payload if type(payload) is dict else None


def _runtime_attestation(raw: bytes, *, helper_sha256: str) -> bytes | None:
    payload = _json_object(raw)
    required = {
        "schema_version",
        "engine",
        "request_revision",
        "language",
        "recognition_level",
        "uses_language_correction",
        "os_build",
        "architecture",
    }
    if payload is None or set(payload) != required:
        return None
    if (
        payload["schema_version"] != _RUNTIME_SCHEMA
        or payload["engine"] != "apple-vision"
        or type(payload["request_revision"]) is not int
        or payload["request_revision"] != 3
        or payload["language"] != "ko-KR"
        or payload["recognition_level"] != "accurate"
        or payload["uses_language_correction"] is not True
        or type(payload["os_build"]) is not str
        or not payload["os_build"]
        or len(payload["os_build"]) > 256
        or type(payload["architecture"]) is not str
        or not payload["architecture"]
        or len(payload["architecture"]) > 64
    ):
        return None
    payload["helper_sha256"] = helper_sha256
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _adapter_lines(raw: bytes, *, image: RasterImage) -> tuple[AdapterLine, ...] | None:
    payload = _json_object(raw)
    if (
        payload is None
        or set(payload) != {"schema_version", "lines"}
        or payload["schema_version"] != _LINES_SCHEMA
        or type(payload["lines"]) is not list
        or len(payload["lines"]) > _MAX_LINES
    ):
        return None
    output: list[AdapterLine] = []
    for item in payload["lines"]:
        if type(item) is not dict or set(item) != {"text", "bbox", "confidence"}:
            return None
        text = item["text"]
        bbox = item["bbox"]
        confidence = item["confidence"]
        if (
            type(text) is not str
            or not text
            or len(text) > 100_000
            or type(bbox) is not list
            or len(bbox) != 4
            or any(type(value) not in (int, float) for value in bbox)
            or type(confidence) not in (int, float)
        ):
            return None
        numeric_bbox = (
            float(bbox[0]),
            float(bbox[1]),
            float(bbox[2]),
            float(bbox[3]),
        )
        x0, y0, x1, y1 = numeric_bbox
        numeric_confidence = float(confidence)
        if (
            not all(math.isfinite(value) for value in numeric_bbox)
            or not math.isfinite(numeric_confidence)
            or not (0.0 <= x0 < x1 <= image.width)
            or not (0.0 <= y0 < y1 <= image.height)
            or not (0.0 <= numeric_confidence <= 1.0)
        ):
            return None
        try:
            output.append(
                AdapterLine(
                    text=text,
                    bbox=numeric_bbox,
                    confidence=numeric_confidence,
                    field_type=_infer_field_type(text),
                )
            )
        except (TypeError, ValueError):
            return None
    return tuple(output)


class AppleVisionOcrAdapter:
    """Execute one digest-pinned helper through a text-safe subprocess boundary."""

    def __init__(
        self,
        *,
        helper_path: Path,
        expected_helper_sha256: str,
        helper_source_path: Path | None = None,
        swift_version: str | None = None,
        sdk_version: str | None = None,
    ) -> None:
        helper_bytes = _read_verified_helper(helper_path, expected_helper_sha256)
        if helper_bytes is None:
            _raise_adapter_error("local Apple Vision helper is invalid")
        self._temporary_directory = tempfile.TemporaryDirectory(
            prefix="sen-qa-apple-vision-"
        )
        executable = _write_private_executable(
            Path(self._temporary_directory.name), helper_bytes
        )
        if executable is None:
            self._temporary_directory.cleanup()
            _raise_adapter_error("local Apple Vision helper is invalid")
        runtime_raw = _run_helper(executable, ("--runtime-info",))
        attestation = (
            _runtime_attestation(runtime_raw, helper_sha256=expected_helper_sha256)
            if runtime_raw is not None
            else None
        )
        if attestation is None:
            self._temporary_directory.cleanup()
            _raise_adapter_error("local Apple Vision runtime is invalid")
        self._executable = executable
        self._runtime_attestation_bytes = attestation
        self._runtime_digest = "sha256:" + hashlib.sha256(attestation).hexdigest()
        self._complete_runtime_provenance_bytes: bytes | None = None
        provided = (
            helper_source_path is not None,
            swift_version is not None,
            sdk_version is not None,
        )
        if any(provided):
            complete: bytes | None = None
            if (
                all(provided)
                and helper_source_path is not None
                and swift_version is not None
                and sdk_version is not None
            ):
                try:
                    complete = self._build_complete_runtime_provenance_bytes(
                        helper_source_path=helper_source_path,
                        swift_version=swift_version,
                        sdk_version=sdk_version,
                    )
                except OcrAdapterError:
                    complete = None
            if complete is None:
                self._temporary_directory.cleanup()
                _raise_adapter_error("local Apple Vision runtime is invalid")
            self._complete_runtime_provenance_bytes = complete

    @property
    def runtime_attestation_bytes(self) -> bytes:
        return self._runtime_attestation_bytes

    @property
    def runtime_digest(self) -> str:
        return self._runtime_digest

    def recognize(self, image: RasterImage) -> tuple[AdapterLine, ...]:
        if (
            type(image) is not RasterImage
            or type(image.width) is not int
            or type(image.height) is not int
            or type(image.rgb_bytes) is not bytes
            or image.width <= 0
            or image.height <= 0
            or image.width * image.height * 3 != len(image.rgb_bytes)
        ):
            _raise_adapter_error("local Apple Vision OCR input is invalid")
        raw = _run_helper(
            self._executable,
            (
                "--width",
                str(image.width),
                "--height",
                str(image.height),
                "--pixel-format",
                "rgb8",
            ),
            input_bytes=image.rgb_bytes,
        )
        lines = _adapter_lines(raw, image=image) if raw is not None else None
        if lines is None:
            _raise_adapter_error("local Apple Vision OCR inference failed")
        return lines

    def _build_complete_runtime_provenance_bytes(
        self,
        *,
        helper_source_path: Path,
        swift_version: str,
        sdk_version: str,
    ) -> bytes:
        """Bind the live helper and Python renderer to complete canonical v3 bytes."""
        source = _read_identity_file(helper_source_path, max_bytes=1024 * 1024)
        adapter_source = _read_identity_file(Path(__file__), max_bytes=1024 * 1024)
        extractor_filename = inspect.getsourcefile(
            build_apple_vision_runtime_provenance
        )
        extractor_path = (
            Path(extractor_filename) if type(extractor_filename) is str else None
        )
        extractor_source = (
            _read_identity_file(extractor_path, max_bytes=4 * 1024 * 1024)
            if extractor_path is not None and extractor_path.name == "extract_ocr.py"
            else None
        )
        base = _json_object(self._runtime_attestation_bytes)
        pymupdf_version: str | None = None
        try:
            pymupdf_version = importlib.metadata.version("PyMuPDF")
        except importlib.metadata.PackageNotFoundError:
            pymupdf_version = None
        if (
            source is None
            or adapter_source is None
            or extractor_source is None
            or base is None
            or type(swift_version) is not str
            or not swift_version
            or len(swift_version) > 128
            or type(sdk_version) is not str
            or not sdk_version
            or len(sdk_version) > 128
            or type(pymupdf_version) is not str
            or not pymupdf_version
        ):
            _raise_adapter_error("local Apple Vision runtime is invalid")
        payload = {
            "schema_version": "sen-qa-apple-vision-runtime-provenance/v2",
            "engine": base.get("engine"),
            "request_revision": base.get("request_revision"),
            "language": base.get("language"),
            "recognition_level": base.get("recognition_level"),
            "uses_language_correction": base.get("uses_language_correction"),
            "macos_build": base.get("os_build"),
            "architecture": base.get("architecture"),
            "swift_version": swift_version,
            "sdk_version": sdk_version,
            "helper_source_sha256": hashlib.sha256(source).hexdigest(),
            "helper_binary_sha256": base.get("helper_sha256"),
            "adapter_sha256": hashlib.sha256(adapter_source).hexdigest(),
            "extractor_pipeline_sha256": hashlib.sha256(extractor_source).hexdigest(),
            "pymupdf_version": pymupdf_version,
        }
        rendered = (
            json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
        provenance_valid = True
        try:
            build_apple_vision_runtime_provenance(rendered)
        except OcrExtractionError:
            provenance_valid = False
        if not provenance_valid:
            _raise_adapter_error("local Apple Vision runtime is invalid")
        return rendered

    def complete_runtime_provenance_bytes(self) -> bytes:
        """Return the constructor-bound complete v3 provenance bytes."""
        if self._complete_runtime_provenance_bytes is None:
            _raise_adapter_error("local Apple Vision runtime is invalid")
        return self._complete_runtime_provenance_bytes

    def close(self) -> None:
        self._temporary_directory.cleanup()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
