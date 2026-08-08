"""Build-only downloader and verifier for locked PaddleOCR model archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

from src.ingestion.extract_ocr import (
    ModelLock,
    ModelLockError,
    load_model_lock,
    validate_installed_models,
)


def _validate_archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        if "\\" in member.name:
            raise ModelLockError("model archive member path is unsafe")
        path = PurePosixPath(member.name)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise ModelLockError("model archive member path is unsafe")
        normalized = path.as_posix().rstrip("/")
        if member.issym() or member.islnk() or member.isdev():
            raise ModelLockError("model archive member type is unsafe")
        if normalized in members:
            raise ModelLockError("duplicate model archive member")
        members[normalized] = member
    return members


def download_locked_archive(source_url: str) -> bytes:
    """Fetch one already lock-validated official archive during image build."""
    try:
        with urlopen(source_url, timeout=120) as response:
            content = response.read()
            if not isinstance(content, bytes):
                raise ModelLockError("locked model download was not bytes")
            return content
    except OSError as error:
        raise ModelLockError("cannot download locked model archive") from error


def prepare_model_staging(
    lock: ModelLock, target: Path, fetch: Callable[[str], bytes]
) -> None:
    """Download, verify, and atomically prepare exactly the locked model files."""
    if target.exists() or target.is_symlink():
        raise ModelLockError("model staging target must not already exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.locked-models-", dir=target.parent)
    )
    prepared = workspace / "prepared"
    prepared.mkdir()
    try:
        for model in lock.models:
            try:
                archive_bytes = fetch(model.source_url)
            except (OSError, ValueError) as error:
                raise ModelLockError("cannot download locked model archive") from error
            if hashlib.sha256(archive_bytes).hexdigest() != model.archive_sha256:
                raise ModelLockError("model archive SHA-256 mismatch")
            try:
                with tarfile.open(
                    fileobj=io.BytesIO(archive_bytes), mode="r:*"
                ) as archive:
                    members = _validate_archive_members(archive)
                    model_directory = prepared / model.name
                    model_directory.mkdir()
                    for locked_file in model.files:
                        archive_path = f"{model.name}/{locked_file.path}"
                        member = members.get(archive_path)
                        if member is None or not member.isfile():
                            raise ModelLockError("missing locked file in model archive")
                        stream = archive.extractfile(member)
                        if stream is None:
                            raise ModelLockError(
                                "cannot read locked file from model archive"
                            )
                        content = stream.read()
                        if hashlib.sha256(content).hexdigest() != locked_file.sha256:
                            raise ModelLockError("model file SHA-256 mismatch")
                        destination = model_directory / locked_file.path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(content)
            except (OSError, tarfile.TarError) as error:
                raise ModelLockError("cannot parse locked model archive") from error
        validate_installed_models(lock, prepared)
        os.replace(prepared, target)
    except (ModelLockError, OSError):
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    try:
        workspace.rmdir()
    except OSError as error:
        raise ModelLockError("cannot clean model preparation workspace") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        lock = load_model_lock(arguments.lock)
        prepare_model_staging(lock, arguments.output, download_locked_archive)
    except (ModelLockError, OSError) as error:
        print(f"models=0 failed=1 error={error}")
        return 1
    print(f"models={len(lock.models)} failed=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
