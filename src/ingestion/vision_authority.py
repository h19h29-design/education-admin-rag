"""Offline operational builder for Apple Vision runtime authority."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import platform
import secrets
import selectors
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

from src.ingestion.apple_vision_ocr import AppleVisionOcrAdapter
from src.ingestion.extract_ocr import (
    OcrAdapterError,
    OcrExtractionError,
    build_apple_vision_runtime_provenance,
)
from src.ingestion.ocr_authority import (
    OcrAuthorityLockError,
    build_ocr_authority_lock,
    canonical_ocr_authority_bytes,
)

_PUBLIC_ERROR: Final = "Apple Vision authority build failed"
_SOURCE_LIMIT: Final = 1024 * 1024
_HELPER_LIMIT: Final = 32 * 1024 * 1024
_PROVENANCE_LIMIT: Final = 64 * 1024
_CONCRETE_PATH_TYPE: Final = type(Path())


class VisionAuthorityError(Exception):
    """A deliberately value-free operational authority failure."""


@dataclass(frozen=True, slots=True)
class RuntimeBuildResult:
    """Public value-free digests for one locally built runtime."""

    helper_sha256: str
    runtime_sha256: str


@dataclass(frozen=True, slots=True)
class AuthorityBuildResult:
    """Public value-free digests and fixed count for one authority lock."""

    file_sha256: str
    self_sha256: str
    entries: int


def _raise_invalid() -> NoReturn:
    raise VisionAuthorityError(_PUBLIC_ERROR)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _read_regular(path: Path, *, maximum: int) -> bytes | None:
    parent_descriptor = _open_safe_parent(path)
    if parent_descriptor is None:
        return None
    descriptor: int | None = None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            return None
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(raw) != before.st_size
            or len(raw) > maximum
            or (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            return None
        return bytes(raw)
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass


def _normalized_absolute(path: Path) -> Path | None:
    if type(path) is not _CONCRETE_PATH_TYPE or not path.is_absolute():
        return None
    normalized = Path(os.path.normpath(os.fspath(path)))
    if not normalized.name or normalized.name in {".", ".."}:
        return None
    return normalized


def _open_safe_parent(path: Path) -> int | None:
    normalized = _normalized_absolute(path)
    if normalized is None:
        return None
    parent = normalized.parent
    current = Path(parent.anchor)
    final_identity: tuple[int, int, int] | None = None
    try:
        root_info = os.lstat(current)
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            return None
        final_identity = (root_info.st_dev, root_info.st_ino, root_info.st_mode)
        for component in parent.parts[1:]:
            current = current / component
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                return None
            final_identity = (info.st_dev, info.st_ino, info.st_mode)
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(parent, flags)
        opened = os.fstat(descriptor)
        if final_identity != (opened.st_dev, opened.st_ino, opened.st_mode):
            os.close(descriptor)
            return None
        return descriptor
    except (OSError, OverflowError, ValueError):
        return None


def _safe_output_key(path: Path) -> tuple[Path, tuple[int, int, str]] | None:
    normalized = _normalized_absolute(path)
    if normalized is None:
        return None
    descriptor = _open_safe_parent(normalized)
    if descriptor is None:
        return None
    try:
        parent = os.fstat(descriptor)
        try:
            os.stat(normalized.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return None
        return normalized, (parent.st_dev, parent.st_ino, normalized.name)
    except (OSError, OverflowError, ValueError):
        return None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_exclusive(path: Path, raw: bytes, *, mode: int) -> None:
    normalized = _normalized_absolute(path)
    parent_descriptor = _open_safe_parent(path)
    if normalized is None or parent_descriptor is None:
        _raise_invalid()
    descriptor: int | None = None
    linked = False
    staging_name = f".{normalized.name}.sen-qa-{secrets.token_hex(16)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    write_failed = False
    try:
        descriptor = os.open(
            staging_name,
            flags,
            mode,
            dir_fd=parent_descriptor,
        )
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                _raise_invalid()
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.close(descriptor)
        descriptor = None
        os.link(
            staging_name,
            normalized.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(staging_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except (OSError, OverflowError, ValueError):
        if linked:
            try:
                os.unlink(normalized.name, dir_fd=parent_descriptor)
            except OSError:
                pass
        write_failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            os.unlink(staging_name, dir_fd=parent_descriptor)
        except OSError:
            pass
        try:
            os.close(parent_descriptor)
        except OSError:
            pass
    if write_failed:
        _raise_invalid()


def _run_bounded(
    arguments: tuple[str, ...], *, maximum: int, timeout_seconds: int
) -> bytes | None:
    if maximum <= 0 or timeout_seconds <= 0:
        return None
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout = process.stdout
        if stdout is None:
            return None
        deadline = time.monotonic() + timeout_seconds
        result = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(stdout.fileno(), min(4096, maximum + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > maximum:
                    return None
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            return None
        return bytes(result)
    except (OSError, OverflowError, ValueError, subprocess.SubprocessError):
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


def _tool_output(arguments: tuple[str, ...]) -> str:
    raw: bytes | None = None
    try:
        raw = _run_bounded(arguments, maximum=4096, timeout_seconds=30)
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raw = None
    if raw is None:
        _raise_invalid()
    decoded: str | None = None
    try:
        decoded = raw.decode("utf-8").strip()
    except UnicodeError:
        decoded = None
    if decoded is None:
        _raise_invalid()
    return decoded


def _swift_version() -> str:
    output = _tool_output(("/usr/bin/xcrun", "swiftc", "--version"))
    matches = [
        line for line in output.splitlines() if line.startswith("Apple Swift version ")
    ]
    if len(matches) != 1 or len(matches[0]) > 128:
        _raise_invalid()
    return matches[0]


def _sdk_version() -> str:
    output = _tool_output(("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-version"))
    if not output or "\n" in output or len(output) > 128:
        _raise_invalid()
    return output


def _checked_in_helper_source() -> tuple[Path, bytes]:
    path = Path(__file__).parents[2] / "scripts" / "apple-vision-ocr.swift"
    raw: bytes | None = None
    try:
        raw = _read_regular(path, maximum=_SOURCE_LIMIT)
    except (OSError, OverflowError, ValueError):
        raw = None
    if raw is None:
        _raise_invalid()
    return path, raw


def build_local_vision_runtime(
    *,
    helper_output: Path,
    provenance_output: Path,
) -> RuntimeBuildResult:
    """Compile and attest the checked-in helper without network access."""

    if platform.system() != "Darwin":
        _raise_invalid()
    helper_spec = _safe_output_key(helper_output)
    provenance_spec = _safe_output_key(provenance_output)
    if (
        helper_spec is None
        or provenance_spec is None
        or helper_spec[1] == provenance_spec[1]
    ):
        _raise_invalid()
    helper_output = helper_spec[0]
    provenance_output = provenance_spec[0]
    checked_source_path, source_raw = _checked_in_helper_source()
    swift_version = _swift_version()
    sdk_version = _sdk_version()
    helper_raw: bytes | None = None
    provenance_raw: bytes | None = None
    runtime_build_failed = False
    try:
        with tempfile.TemporaryDirectory(
            prefix=".sen-qa-vision-authority-",
            dir=helper_output.parent,
        ) as name:
            temporary_root = Path(name)
            source_path = temporary_root / "apple-vision-ocr.swift"
            helper_path = temporary_root / "apple-vision-ocr"
            _write_exclusive(source_path, source_raw, mode=0o600)
            subprocess.run(
                (
                    "/usr/bin/xcrun",
                    "swiftc",
                    "-O",
                    str(source_path),
                    "-o",
                    str(helper_path),
                ),
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            helper_raw = _read_regular(helper_path, maximum=_HELPER_LIMIT)
            if helper_raw is None:
                _raise_invalid()
            helper_sha256 = hashlib.sha256(helper_raw).hexdigest()
            with AppleVisionOcrAdapter(
                helper_path=helper_path,
                expected_helper_sha256=helper_sha256,
                helper_source_path=source_path,
                swift_version=swift_version,
                sdk_version=sdk_version,
            ) as adapter:
                provenance_raw = adapter.complete_runtime_provenance_bytes()
                copied_source_raw = _read_regular(source_path, maximum=_SOURCE_LIMIT)
                live_checked_source_raw = _read_regular(
                    checked_source_path, maximum=_SOURCE_LIMIT
                )
                runtime = build_apple_vision_runtime_provenance(provenance_raw)
                if (
                    copied_source_raw is None
                    or live_checked_source_raw is None
                    or not hmac.compare_digest(copied_source_raw, source_raw)
                    or not hmac.compare_digest(live_checked_source_raw, source_raw)
                    or not hmac.compare_digest(
                        runtime.helper_source_sha256,
                        hashlib.sha256(source_raw).hexdigest(),
                    )
                ):
                    _raise_invalid()
    except (
        OcrAdapterError,
        OcrExtractionError,
        OSError,
        subprocess.SubprocessError,
        VisionAuthorityError,
    ):
        runtime_build_failed = True
    if runtime_build_failed:
        _raise_invalid()
    if (
        helper_raw is None
        or provenance_raw is None
        or len(provenance_raw) > _PROVENANCE_LIMIT
    ):
        _raise_invalid()
    committed: list[Path] = []
    publication_failed = False
    try:
        _write_exclusive(helper_output, helper_raw, mode=0o700)
        committed.append(helper_output)
        _write_exclusive(provenance_output, provenance_raw, mode=0o600)
        committed.append(provenance_output)
        installed_helper = _read_regular(helper_output, maximum=_HELPER_LIMIT)
        installed_provenance = _read_regular(
            provenance_output, maximum=_PROVENANCE_LIMIT
        )
        if (
            installed_helper is None
            or installed_provenance is None
            or not hmac.compare_digest(
                hashlib.sha256(installed_helper).hexdigest(),
                hashlib.sha256(helper_raw).hexdigest(),
            )
            or not hmac.compare_digest(
                hashlib.sha256(installed_provenance).hexdigest(),
                hashlib.sha256(provenance_raw).hexdigest(),
            )
            or stat.S_IMODE(helper_output.stat().st_mode) != 0o700
            or stat.S_IMODE(provenance_output.stat().st_mode) != 0o600
        ):
            _raise_invalid()
    except (OSError, VisionAuthorityError):
        for path in committed:
            try:
                path.unlink()
            except OSError:
                pass
        publication_failed = True
    if publication_failed:
        _raise_invalid()
    return RuntimeBuildResult(
        helper_sha256=hashlib.sha256(helper_raw).hexdigest(),
        runtime_sha256=hashlib.sha256(provenance_raw).hexdigest(),
    )


def _verified_runtime_fingerprint(path: Path, expected_sha256: str) -> str:
    if type(path) is not _CONCRETE_PATH_TYPE or not _is_sha256(expected_sha256):
        _raise_invalid()
    raw: bytes | None = None
    try:
        raw = _read_regular(path, maximum=_PROVENANCE_LIMIT)
    except (OSError, OverflowError, ValueError):
        raw = None
    if raw is None or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_sha256
    ):
        _raise_invalid()
    provenance_is_valid = True
    try:
        build_apple_vision_runtime_provenance(raw)
    except OcrExtractionError:
        provenance_is_valid = False
    if not provenance_is_valid:
        _raise_invalid()
    return "sha256:" + expected_sha256


def build_vision_authority_lock(
    *,
    runtime_2024: Path,
    expected_runtime_2024_sha256: str,
    runtime_2025: Path,
    expected_runtime_2025_sha256: str,
    authority_output: Path,
) -> AuthorityBuildResult:
    """Bind the canonical Paddle 2023 and independently pinned Vision runtimes."""

    authority_spec = _safe_output_key(authority_output)
    if authority_spec is None:
        _raise_invalid()
    authority_output = authority_spec[0]
    fingerprint_2024 = _verified_runtime_fingerprint(
        runtime_2024, expected_runtime_2024_sha256
    )
    fingerprint_2025 = _verified_runtime_fingerprint(
        runtime_2025, expected_runtime_2025_sha256
    )
    lock = None
    raw = None
    try:
        lock = build_ocr_authority_lock(
            vision_2024_runtime_fingerprint=fingerprint_2024,
            vision_2025_runtime_fingerprint=fingerprint_2025,
        )
        raw = canonical_ocr_authority_bytes(lock)
    except OcrAuthorityLockError:
        lock = None
        raw = None
    if lock is None or raw is None:
        _raise_invalid()
    _write_exclusive(authority_output, raw, mode=0o600)
    try:
        installed = _read_regular(authority_output, maximum=_PROVENANCE_LIMIT)
        installed_is_valid = (
            installed is not None
            and hmac.compare_digest(
                hashlib.sha256(installed).hexdigest(), hashlib.sha256(raw).hexdigest()
            )
            and stat.S_IMODE(authority_output.stat().st_mode) == 0o600
        )
    except OSError:
        installed_is_valid = False
    if not installed_is_valid:
        try:
            authority_output.unlink()
        except OSError:
            pass
        _raise_invalid()
    return AuthorityBuildResult(
        file_sha256=hashlib.sha256(raw).hexdigest(),
        self_sha256=lock.self_sha256,
        entries=len(lock.entries),
    )


class _ValueFreeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> NoReturn:
        _raise_invalid()


def _argument_parser() -> argparse.ArgumentParser:
    parser = _ValueFreeArgumentParser(prog="vision-authority")
    commands = parser.add_subparsers(dest="command", required=True)
    runtime = commands.add_parser("build-runtime")
    runtime.add_argument("--helper-output", required=True, type=Path)
    runtime.add_argument("--provenance-output", required=True, type=Path)
    lock = commands.add_parser("build-lock")
    lock.add_argument("--runtime-2024", required=True, type=Path)
    lock.add_argument("--expected-runtime-2024-sha256", required=True)
    lock.add_argument("--runtime-2025", required=True, type=Path)
    lock.add_argument("--expected-runtime-2025-sha256", required=True)
    lock.add_argument("--authority-output", required=True, type=Path)
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Run the value-free offline authority command boundary."""

    try:
        parsed = _argument_parser().parse_args(arguments)
        if parsed.command == "build-runtime":
            result = build_local_vision_runtime(
                helper_output=parsed.helper_output,
                provenance_output=parsed.provenance_output,
            )
            print(
                "built=2"
                f" helper_sha256={result.helper_sha256}"
                f" runtime_sha256={result.runtime_sha256}"
            )
        elif parsed.command == "build-lock":
            authority = build_vision_authority_lock(
                runtime_2024=parsed.runtime_2024,
                expected_runtime_2024_sha256=parsed.expected_runtime_2024_sha256,
                runtime_2025=parsed.runtime_2025,
                expected_runtime_2025_sha256=parsed.expected_runtime_2025_sha256,
                authority_output=parsed.authority_output,
            )
            print(
                "built=1"
                f" entries={authority.entries}"
                f" file_sha256={authority.file_sha256}"
                f" self_sha256={authority.self_sha256}"
            )
        else:
            _raise_invalid()
    except (VisionAuthorityError, AttributeError, TypeError, ValueError):
        print("failed=1 error_code=vision_authority_failed")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
