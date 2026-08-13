"""Download only an externally pinned embedding closure during image build."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from src.corpus.chunking import (
    ChunkingError,
    load_embedding_model_lock,
    verify_embedding_cache,
)

_READ_SIZE = 1024 * 1024


def _download(source_url: str, target: Path, *, size: int, sha256: str) -> bool:
    descriptor: int | None = None
    response = None
    failed = False
    digest = hashlib.sha256()
    written = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        request = urllib.request.Request(
            source_url, headers={"User-Agent": "sen-qa-indexer-build/1"}
        )
        # Immutable Hugging Face resolve URLs legitimately redirect to its cache/CDN.
        # The reviewed source URL plus exact byte size and SHA-256 remain authoritative.
        response = urllib.request.urlopen(request, timeout=120)
        while not failed:
            chunk = response.read(min(_READ_SIZE, size - written + 1))
            if not chunk:
                break
            written += len(chunk)
            if written > size:
                failed = True
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    failed = True
                    break
                view = view[count:]
        os.fsync(descriptor)
    except (OSError, TimeoutError, urllib.error.URLError, ValueError):
        failed = True
    finally:
        if response is not None:
            response.close()
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    return (
        not failed
        and written == size
        and hmac.compare_digest(digest.hexdigest(), sha256)
    )


def prepare(lock_path: Path, output: Path, *, expected_lock_sha256: str) -> None:
    """Materialize and verify the exact full model closure without overwriting output."""
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise RuntimeError("embedding output is invalid")
    lock = load_embedding_model_lock(lock_path)
    if not hmac.compare_digest(lock.fingerprint_sha256, expected_lock_sha256):
        raise RuntimeError("embedding lock fingerprint is invalid")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent))
    failed = False
    try:
        for locked_file in lock.files:
            if not _download(
                locked_file.source_url,
                temporary / locked_file.path,
                size=locked_file.size,
                sha256=locked_file.sha256,
            ):
                failed = True
                break
        if not failed:
            verify_embedding_cache(
                lock,
                temporary,
                scope="full",
                expected_lock_sha256=expected_lock_sha256,
            )
            if output.exists() or output.is_symlink():
                failed = True
            else:
                os.rename(temporary, output)
    except (ChunkingError, OSError, RuntimeError, ValueError):
        failed = True
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    if failed:
        raise RuntimeError("embedding preparation failed") from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-lock-sha256", required=True)
    arguments = parser.parse_args()
    failed = False
    try:
        prepare(
            arguments.lock,
            arguments.output,
            expected_lock_sha256=arguments.expected_lock_sha256,
        )
    except (ChunkingError, OSError, RuntimeError, TypeError, ValueError):
        failed = True
    if failed:
        raise SystemExit("embedding preparation failed") from None


if __name__ == "__main__":
    main()
