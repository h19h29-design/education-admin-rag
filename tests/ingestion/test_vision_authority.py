from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import traceback
from pathlib import Path
from typing import NoReturn, Self

import pytest

_PRIVATE_SENTINEL = "private-vision-authority-sentinel"


def _assert_value_free_exception(error: Exception) -> None:
    rendered = "".join(traceback.format_exception(error))
    assert str(error) == "Apple Vision authority build failed"
    assert _PRIVATE_SENTINEL not in str(error)
    assert _PRIVATE_SENTINEL not in repr(error)
    assert _PRIVATE_SENTINEL not in rendered
    assert error.__cause__ is None
    assert error.__context__ is None


def _runtime_bytes(*, macos_build: str) -> bytes:
    return (
        json.dumps(
            {
                "adapter_sha256": "a" * 64,
                "architecture": "arm64",
                "engine": "apple-vision",
                "extractor_pipeline_sha256": "b" * 64,
                "helper_binary_sha256": "c" * 64,
                "helper_source_sha256": "d" * 64,
                "language": "ko-KR",
                "macos_build": macos_build,
                "pymupdf_version": "1.28.2",
                "recognition_level": "accurate",
                "request_revision": 3,
                "schema_version": "sen-qa-apple-vision-runtime-provenance/v2",
                "sdk_version": "26.5",
                "swift_version": "Apple Swift version 6.3.2",
                "uses_language_correction": True,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_build_runtime_compiles_live_helper_and_writes_canonical_private_outputs(
    tmp_path: Path,
) -> None:
    """Catches a runtime builder using claimed tool data or noncanonical files."""
    from src.ingestion.vision_authority import build_local_vision_runtime

    helper = tmp_path / "apple-vision-ocr"
    provenance = tmp_path / "vision-runtime-provenance.json"

    result = build_local_vision_runtime(
        helper_output=helper,
        provenance_output=provenance,
    )

    helper_raw = helper.read_bytes()
    provenance_raw = provenance.read_bytes()
    payload = json.loads(provenance_raw)
    assert result.helper_sha256 == hashlib.sha256(helper_raw).hexdigest()
    assert result.runtime_sha256 == hashlib.sha256(provenance_raw).hexdigest()
    assert payload["schema_version"] == "sen-qa-apple-vision-runtime-provenance/v2"
    assert payload["helper_binary_sha256"] == result.helper_sha256
    assert payload["swift_version"].startswith("Apple Swift version ")
    assert payload["sdk_version"]
    assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert stat.S_IMODE(provenance.stat().st_mode) == 0o600


def test_build_lock_binds_two_independently_hashed_canonical_runtimes(
    tmp_path: Path,
) -> None:
    """Catches either Vision year losing its independently verified runtime."""
    from src.ingestion.vision_authority import build_vision_authority_lock

    runtime_2024 = tmp_path / "runtime-2024.json"
    runtime_2025 = tmp_path / "runtime-2025.json"
    output = tmp_path / "ocr-authority-lock.json"
    raw_2024 = _runtime_bytes(macos_build="Version 26.4 (Build A)")
    raw_2025 = _runtime_bytes(macos_build="Version 26.4 (Build B)")
    runtime_2024.write_bytes(raw_2024)
    runtime_2025.write_bytes(raw_2025)

    result = build_vision_authority_lock(
        runtime_2024=runtime_2024,
        expected_runtime_2024_sha256=hashlib.sha256(raw_2024).hexdigest(),
        runtime_2025=runtime_2025,
        expected_runtime_2025_sha256=hashlib.sha256(raw_2025).hexdigest(),
        authority_output=output,
    )

    raw = output.read_bytes()
    payload = json.loads(raw)
    assert result.entries == 3
    assert result.file_sha256 == hashlib.sha256(raw).hexdigest()
    assert result.self_sha256 == payload["self_sha256"]
    assert [entry["year"] for entry in payload["entries"]] == [2023, 2024, 2025]
    assert payload["entries"][1]["authority"] == (
        "sha256:" + hashlib.sha256(raw_2024).hexdigest()
    )
    assert payload["entries"][2]["authority"] == (
        "sha256:" + hashlib.sha256(raw_2025).hexdigest()
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_module_cli_lock_reports_only_fixed_counts_and_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches local paths or provenance values escaping the command boundary."""
    from src.ingestion.vision_authority import main

    runtime_2024 = tmp_path / "private-2024-secret-name.json"
    runtime_2025 = tmp_path / "private-2025-secret-name.json"
    output = tmp_path / "private-authority-secret-name.json"
    raw_2024 = _runtime_bytes(macos_build="Version 26.4 (Build A)")
    raw_2025 = _runtime_bytes(macos_build="Version 26.4 (Build B)")
    runtime_2024.write_bytes(raw_2024)
    runtime_2025.write_bytes(raw_2025)

    exit_code = main(
        [
            "build-lock",
            "--runtime-2024",
            str(runtime_2024),
            "--expected-runtime-2024-sha256",
            hashlib.sha256(raw_2024).hexdigest(),
            "--runtime-2025",
            str(runtime_2025),
            "--expected-runtime-2025-sha256",
            hashlib.sha256(raw_2025).hexdigest(),
            "--authority-output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    fields = captured.out.strip().split()
    assert exit_code == 0
    assert captured.err == ""
    assert fields[0:2] == ["built=1", "entries=3"]
    assert fields[2].startswith("file_sha256=") and len(fields[2]) == 76
    assert fields[3].startswith("self_sha256=") and len(fields[3]) == 76
    assert "secret-name" not in captured.out
    assert "Version" not in captured.out


def test_module_cli_non_darwin_failure_is_fixed_and_creates_no_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches platform failures reading private paths or leaking their values."""
    from src.ingestion import vision_authority

    monkeypatch.setattr(vision_authority.platform, "system", lambda: "Linux")
    helper = tmp_path / "private-helper-secret-name"
    runtime = tmp_path / "private-runtime-secret-name"

    exit_code = vision_authority.main(
        [
            "build-runtime",
            "--helper-output",
            str(helper),
            "--provenance-output",
            str(runtime),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == "failed=1 error_code=vision_authority_failed\n"
    assert captured.err == ""
    assert not helper.exists()
    assert not runtime.exists()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_runtime_rejects_intermediate_output_symlink_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches output publication escaping through a symlinked parent path."""
    from src.ingestion import vision_authority

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(
        vision_authority,
        "_swift_version",
        lambda: pytest.fail("tools must not run for an unsafe output path"),
    )

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=linked / "apple-vision-ocr",
            provenance_output=linked / "runtime.json",
        )

    assert str(caught.value) == "Apple Vision authority build failed"
    assert list(real.iterdir()) == []


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_runtime_rejects_aliasing_outputs_before_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches lexical path aliases targeting the same final output."""
    from src.ingestion import vision_authority

    directory = tmp_path / "outputs"
    directory.mkdir()
    monkeypatch.setattr(
        vision_authority,
        "_swift_version",
        lambda: pytest.fail("tools must not run for overlapping outputs"),
    )

    with pytest.raises(vision_authority.VisionAuthorityError):
        vision_authority.build_local_vision_runtime(
            helper_output=directory / "runtime.json",
            provenance_output=directory / "nested" / ".." / "runtime.json",
        )

    assert list(directory.iterdir()) == []


def test_lock_rejects_fifo_runtime_without_blocking_or_partial_output(
    tmp_path: Path,
) -> None:
    """Catches an unbounded blocking read from a hostile runtime node."""
    from src.ingestion.vision_authority import (
        VisionAuthorityError,
        build_vision_authority_lock,
    )

    fifo = tmp_path / "runtime.fifo"
    os.mkfifo(fifo)
    runtime = tmp_path / "runtime.json"
    runtime.write_bytes(_runtime_bytes(macos_build="Version 26.4 (Build B)"))
    output = tmp_path / "authority.json"

    with pytest.raises(VisionAuthorityError) as caught:
        build_vision_authority_lock(
            runtime_2024=fifo,
            expected_runtime_2024_sha256="a" * 64,
            runtime_2025=runtime,
            expected_runtime_2025_sha256=hashlib.sha256(
                runtime.read_bytes()
            ).hexdigest(),
            authority_output=output,
        )

    assert str(caught.value) == "Apple Vision authority build failed"
    assert not output.exists()


def test_bounded_process_output_terminates_a_hostile_producer() -> None:
    """Catches tool discovery buffering attacker-sized stdout in memory."""
    from src.ingestion.vision_authority import _run_bounded

    raw = _run_bounded(
        (sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 10000)"),
        maximum=4096,
        timeout_seconds=10,
    )

    assert raw is None


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_runtime_rejects_post_publish_hash_drift_and_removes_both_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches returning success after a published helper no longer matches its SHA."""
    from src.ingestion import vision_authority

    helper = tmp_path / "apple-vision-ocr"
    provenance = tmp_path / "runtime.json"
    original_write = vision_authority._write_exclusive

    def write_then_mutate(path: Path, raw: bytes, *, mode: int) -> None:
        original_write(path, raw, mode=mode)
        if path == helper:
            path.write_bytes(b"changed-after-publish")

    monkeypatch.setattr(vision_authority, "_write_exclusive", write_then_mutate)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=helper,
            provenance_output=provenance,
        )

    assert str(caught.value) == "Apple Vision authority build failed"
    assert not helper.exists()
    assert not provenance.exists()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_runtime_sanitizes_post_publish_metadata_failure_and_removes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a final metadata race leaking an OS error or leaving one output."""
    from src.ingestion import vision_authority

    helper = tmp_path / "apple-vision-ocr"
    provenance = tmp_path / "runtime.json"
    monkeypatch.setattr(
        vision_authority.stat,
        "S_IMODE",
        lambda _mode: (_ for _ in ()).throw(OSError("private-metadata-value")),
    )

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=helper,
            provenance_output=provenance,
        )

    assert str(caught.value) == "Apple Vision authority build failed"
    assert "private-metadata-value" not in str(caught.value)
    assert not helper.exists()
    assert not provenance.exists()


def test_tool_decode_failure_has_no_private_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a Unicode decoder failure surviving behind the public error."""
    from src.ingestion import vision_authority

    monkeypatch.setattr(
        vision_authority,
        "_run_bounded",
        lambda *_args, **_kwargs: b"\xff" + _PRIVATE_SENTINEL.encode("ascii"),
    )

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority._tool_output(("ignored",))

    _assert_value_free_exception(caught.value)


def test_tool_oserror_has_no_private_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a tool-launch OS error surviving behind the public error."""
    from src.ingestion import vision_authority

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority, "_run_bounded", fail)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority._tool_output(("ignored",))

    _assert_value_free_exception(caught.value)


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_compile_failure_has_no_private_exception_chain_or_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches compiler exception context or a half-published runtime."""
    from src.ingestion import vision_authority

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority.subprocess, "run", fail)
    helper = tmp_path / "helper"
    provenance = tmp_path / "runtime.json"

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=helper,
            provenance_output=provenance,
        )

    _assert_value_free_exception(caught.value)
    assert not helper.exists()
    assert not provenance.exists()


def test_runtime_read_failure_has_no_private_exception_chain_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a runtime read exception crossing the authority boundary."""
    from src.ingestion import vision_authority

    output = tmp_path / "authority.json"

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority, "_read_regular", fail)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_vision_authority_lock(
            runtime_2024=tmp_path / "2024.json",
            expected_runtime_2024_sha256="a" * 64,
            runtime_2025=tmp_path / "2025.json",
            expected_runtime_2025_sha256="b" * 64,
            authority_output=output,
        )

    _assert_value_free_exception(caught.value)
    assert not output.exists()


def test_exclusive_write_failure_has_no_private_exception_chain_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a filesystem write error retained by the public exception."""
    from src.ingestion import vision_authority

    output = tmp_path / "output.json"

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        raise OSError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority.os, "write", fail)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority._write_exclusive(output, b"private", mode=0o600)

    _assert_value_free_exception(caught.value)
    assert not output.exists()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_postpublish_failure_has_no_private_exception_chain_or_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a final metadata error retained after both files were published."""
    from src.ingestion import vision_authority

    helper = tmp_path / "helper"
    provenance = tmp_path / "runtime.json"

    def fail(_mode: int) -> NoReturn:
        raise OSError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority.stat, "S_IMODE", fail)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=helper,
            provenance_output=provenance,
        )

    _assert_value_free_exception(caught.value)
    assert not helper.exists()
    assert not provenance.exists()


def test_lock_builder_failure_has_no_private_exception_chain_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches authority-builder details surviving behind the public error."""
    from src.ingestion import vision_authority

    runtime = tmp_path / "runtime.json"
    raw = _runtime_bytes(macos_build="Version 26.4 (Build A)")
    runtime.write_bytes(raw)
    output = tmp_path / "authority.json"

    def fail(**_kwargs: object) -> NoReturn:
        raise vision_authority.OcrAuthorityLockError(_PRIVATE_SENTINEL)

    monkeypatch.setattr(vision_authority, "build_ocr_authority_lock", fail)

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_vision_authority_lock(
            runtime_2024=runtime,
            expected_runtime_2024_sha256=hashlib.sha256(raw).hexdigest(),
            runtime_2025=runtime,
            expected_runtime_2025_sha256=hashlib.sha256(raw).hexdigest(),
            authority_output=output,
        )

    _assert_value_free_exception(caught.value)
    assert not output.exists()


@pytest.mark.skipif(platform.system() != "Darwin", reason="Apple Vision is macOS-only")
def test_runtime_rejects_temporary_source_aba_during_attestation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches provenance hashing B after the verified source returns to A."""
    from src.ingestion import vision_authority

    real_adapter = vision_authority.AppleVisionOcrAdapter

    class AbaAdapter:
        def __init__(self, **kwargs: object) -> None:
            source = kwargs["helper_source_path"]
            assert isinstance(source, Path)
            self._source = source
            self._initial = source.read_bytes()
            before = source.stat()
            self._times = (before.st_atime_ns, before.st_mtime_ns)
            source.write_bytes(b" " * len(self._initial))
            self._adapter = real_adapter(**kwargs)

        def __enter__(self) -> Self:
            self._adapter.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._adapter.__exit__(*args)

        def complete_runtime_provenance_bytes(self) -> bytes:
            raw = self._adapter.complete_runtime_provenance_bytes()
            self._source.write_bytes(self._initial)
            os.utime(self._source, ns=self._times)
            return raw

    monkeypatch.setattr(vision_authority, "AppleVisionOcrAdapter", AbaAdapter)
    helper = tmp_path / "helper"
    provenance = tmp_path / "runtime.json"

    with pytest.raises(vision_authority.VisionAuthorityError) as caught:
        vision_authority.build_local_vision_runtime(
            helper_output=helper,
            provenance_output=provenance,
        )

    _assert_value_free_exception(caught.value)
    assert not helper.exists()
    assert not provenance.exists()
