from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import traceback
from pathlib import Path, PosixPath

import pytest

from src.ingestion.apple_vision_ocr import AppleVisionOcrAdapter
from src.ingestion.extract_ocr import (
    OcrAdapterError,
    RasterImage,
    build_apple_vision_runtime_provenance,
)


class _HostilePath(PosixPath):
    def __fspath__(self) -> str:
        raise RuntimeError("PRIVATE_VISION_PATH_BOMB")


def _write_helper(path: Path, *, response: dict[str, object], stderr: str = "") -> str:
    runtime = {
        "schema_version": "sen-qa-apple-vision-runtime/v1",
        "engine": "apple-vision",
        "request_revision": 3,
        "language": "ko-KR",
        "recognition_level": "accurate",
        "uses_language_correction": True,
        "os_build": "test-build",
        "architecture": "test-arch",
    }
    source = f"""#!/usr/bin/env python3
import json
import sys

if sys.argv[1:] == [\"--runtime-info\"]:
    print(json.dumps({runtime!r}, sort_keys=True, separators=(\",\", \":\")))
    raise SystemExit(0)
if sys.argv[1:] != [\"--width\", \"2\", \"--height\", \"1\", \"--pixel-format\", \"rgb8\"]:
    raise SystemExit(3)
if len(sys.stdin.buffer.read()) != 6:
    raise SystemExit(4)
sys.stderr.write({stderr!r})
print(json.dumps({response!r}, ensure_ascii=False, sort_keys=True, separators=(\",\", \":\")))
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_oversized_streaming_helper(
    path: Path,
    *,
    marker_path: Path,
    pid_path: Path,
) -> str:
    runtime = {
        "schema_version": "sen-qa-apple-vision-runtime/v1",
        "engine": "apple-vision",
        "request_revision": 3,
        "language": "ko-KR",
        "recognition_level": "accurate",
        "uses_language_correction": True,
        "os_build": "test-build",
        "architecture": "test-arch",
    }
    source = f"""#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

if sys.argv[1:] == [\"--runtime-info\"]:
    print(json.dumps({runtime!r}, sort_keys=True, separators=(\",\", \":\")))
    raise SystemExit(0)
if sys.argv[1:] != [\"--width\", \"2048\", \"--height\", \"2048\", \"--pixel-format\", \"rgb8\"]:
    raise SystemExit(3)
if len(sys.stdin.buffer.read(6)) != 6:
    raise SystemExit(4)
with open({str(pid_path)!r}, \"w\", encoding=\"ascii\") as stream:
    stream.write(str(os.getpid()))
    stream.flush()
    os.fsync(stream.fileno())
chunk = b\"x\" * 65536
for _ in range(257):
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
time.sleep(0.25)
Path({str(marker_path)!r}).write_text(\"PRIVATE_POST_OVERFLOW_MARKER\", encoding=\"ascii\")
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _valid_response() -> dict[str, object]:
    return {
        "schema_version": "sen-qa-apple-vision-lines/v1",
        "lines": [
            {
                "text": "질문 합성 문장",
                "bbox": [0.25, 0.0, 1.75, 1.0],
                "confidence": 0.875,
            }
        ],
    }


def test_adapter_returns_validated_pixel_lines_and_bound_runtime_digest(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "vision-helper"
    helper_sha256 = _write_helper(helper, response=_valid_response())

    with AppleVisionOcrAdapter(
        helper_path=helper,
        expected_helper_sha256=helper_sha256,
    ) as adapter:
        lines = adapter.recognize(
            RasterImage(width=2, height=1, rgb_bytes=b"\x00\x01\x02\x03\x04\x05")
        )
        runtime_digest = adapter.runtime_digest
        runtime = json.loads(adapter.runtime_attestation_bytes)

    assert len(lines) == 1
    assert lines[0].text == "질문 합성 문장"
    assert lines[0].field_type == "question"
    assert lines[0].bbox == (0.25, 0.0, 1.75, 1.0)
    assert lines[0].confidence == 0.875
    assert runtime["helper_sha256"] == helper_sha256
    assert runtime["engine"] == "apple-vision"
    assert (
        runtime_digest
        == "sha256:" + hashlib.sha256(adapter.runtime_attestation_bytes).hexdigest()
    )


def test_adapter_rejects_wrong_helper_digest_before_execution(tmp_path: Path) -> None:
    helper = tmp_path / "vision-helper"
    _write_helper(helper, response=_valid_response())

    with pytest.raises(OcrAdapterError, match="local Apple Vision helper is invalid"):
        AppleVisionOcrAdapter(
            helper_path=helper,
            expected_helper_sha256="0" * 64,
        )


def test_adapter_failure_does_not_retain_recognized_text_or_helper_error(
    tmp_path: Path,
) -> None:
    private = "PRIVATE_VISION_SENTINEL"
    helper = tmp_path / "vision-helper"
    helper_sha256 = _write_helper(
        helper,
        response={"schema_version": "wrong", "lines": [private]},
        stderr=private,
    )

    with (
        AppleVisionOcrAdapter(
            helper_path=helper,
            expected_helper_sha256=helper_sha256,
        ) as adapter,
        pytest.raises(OcrAdapterError) as caught,
    ):
        adapter.recognize(
            RasterImage(width=2, height=1, rgb_bytes=private.encode()[:6])
        )

    rendered = "\n".join(
        (
            str(caught.value),
            repr(caught.value),
            "".join(
                traceback.format_exception(
                    type(caught.value), caught.value, caught.value.__traceback__
                )
            ),
            repr(caught.value.__cause__),
            repr(caught.value.__context__),
        )
    )
    assert private not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_adapter_terminates_helper_before_oversized_stdout_can_complete(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "vision-helper"
    marker = tmp_path / "post-overflow-marker"
    pid_file = tmp_path / "helper-pid"
    helper_sha256 = _write_oversized_streaming_helper(
        helper,
        marker_path=marker,
        pid_path=pid_file,
    )

    with (
        AppleVisionOcrAdapter(
            helper_path=helper,
            expected_helper_sha256=helper_sha256,
        ) as adapter,
        pytest.raises(OcrAdapterError) as caught,
    ):
        adapter.recognize(
            RasterImage(
                width=2048,
                height=2048,
                rgb_bytes=b"\x00" * (2048 * 2048 * 3),
            )
        )

    helper_pid = int(pid_file.read_text(encoding="ascii"))
    with pytest.raises(ProcessLookupError):
        os.kill(helper_pid, 0)
    assert not marker.exists()
    assert str(caught.value) == "local Apple Vision OCR inference failed"
    assert "PRIVATE_POST_OVERFLOW_MARKER" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_adapter_rejects_symlinked_helper(tmp_path: Path) -> None:
    target = tmp_path / "target-helper"
    helper_sha256 = _write_helper(target, response=_valid_response())
    link = tmp_path / "vision-helper"
    os.symlink(target, link)

    with pytest.raises(OcrAdapterError, match="local Apple Vision helper is invalid"):
        AppleVisionOcrAdapter(
            helper_path=link,
            expected_helper_sha256=helper_sha256,
        )


def test_adapter_rejects_path_subclass_before_fspath_can_run() -> None:
    with pytest.raises(OcrAdapterError) as caught:
        AppleVisionOcrAdapter(
            helper_path=_HostilePath("unused"),
            expected_helper_sha256="0" * 64,
        )

    assert "PRIVATE_VISION_PATH_BOMB" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_complete_runtime_provenance_binds_source_binary_adapter_and_renderer(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "vision-helper"
    helper_sha256 = _write_helper(helper, response=_valid_response())
    helper_source = tmp_path / "helper.swift"
    helper_source.write_text("// synthetic helper source\n", encoding="utf-8")

    with AppleVisionOcrAdapter(
        helper_path=helper,
        expected_helper_sha256=helper_sha256,
        helper_source_path=helper_source,
        swift_version="Swift 6.3.2",
        sdk_version="macosx26.4",
    ) as adapter:
        raw = adapter.complete_runtime_provenance_bytes()

    provenance = build_apple_vision_runtime_provenance(raw)
    payload = provenance.model_dump(mode="json")
    assert payload["schema_version"] == "sen-qa-apple-vision-runtime-provenance/v2"
    assert (
        payload["helper_source_sha256"]
        == hashlib.sha256(helper_source.read_bytes()).hexdigest()
    )
    assert payload["helper_binary_sha256"] == helper_sha256
    assert (
        payload["adapter_sha256"]
        == hashlib.sha256(
            Path("src/ingestion/apple_vision_ocr.py").read_bytes()
        ).hexdigest()
    )
    assert (
        payload["extractor_pipeline_sha256"]
        == hashlib.sha256(Path("src/ingestion/extract_ocr.py").read_bytes()).hexdigest()
    )
    assert payload["pymupdf_version"]
    assert payload["macos_build"] == "test-build"
    assert payload["architecture"] == "test-arch"


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS-only")
def test_checked_in_swift_helper_compiles_and_reports_the_pinned_runtime_contract(
    tmp_path: Path,
) -> None:
    source = Path("scripts/apple-vision-ocr.swift")
    executable = tmp_path / "apple-vision-ocr"

    compiled = subprocess.run(
        ("swiftc", "-O", str(source), "-o", str(executable)),
        check=False,
        capture_output=True,
        timeout=120,
    )
    assert compiled.returncode == 0
    runtime = subprocess.run(
        (str(executable), "--runtime-info"),
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert runtime.returncode == 0
    payload = json.loads(runtime.stdout)
    assert set(payload) == {
        "architecture",
        "engine",
        "language",
        "os_build",
        "recognition_level",
        "request_revision",
        "schema_version",
        "uses_language_correction",
    }
    assert payload["schema_version"] == "sen-qa-apple-vision-runtime/v1"
    assert payload["engine"] == "apple-vision"
    assert payload["language"] == "ko-KR"
    assert payload["request_revision"] == 3
    assert payload["recognition_level"] == "accurate"
    assert payload["uses_language_correction"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision is macOS-only")
def test_checked_in_swift_helper_clamps_only_tiny_vision_bbox_overflow(
    tmp_path: Path,
) -> None:
    """Catches one slightly oversized Vision box quarantining an entire page."""
    source = Path("scripts/apple-vision-ocr.swift")
    executable = tmp_path / "apple-vision-geometry-test"
    compiled = subprocess.run(
        (
            "swiftc",
            "-O",
            "-D",
            "SEN_QA_GEOMETRY_TEST",
            str(source),
            "-o",
            str(executable),
        ),
        check=False,
        capture_output=True,
        timeout=120,
    )
    assert compiled.returncode == 0

    tiny_overflow = subprocess.run(
        (
            str(executable),
            "--geometry-test",
            "0.1",
            "0.2",
            "1.004",
            "0.4",
            "100",
            "100",
        ),
        check=False,
        capture_output=True,
        timeout=30,
    )
    large_overflow = subprocess.run(
        (
            str(executable),
            "--geometry-test",
            "0.1",
            "0.2",
            "1.006",
            "0.4",
            "100",
            "100",
        ),
        check=False,
        capture_output=True,
        timeout=30,
    )

    assert tiny_overflow.returncode == 0
    assert json.loads(tiny_overflow.stdout) == {"bbox": [10, 60, 100, 80]}
    assert large_overflow.returncode == 1
    assert large_overflow.stdout == b""
