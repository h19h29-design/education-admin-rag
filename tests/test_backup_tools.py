from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path
from typing import Self

import pytest

import docker.prepare_backup_tools as prepare
from tests.test_ingestion_dockerfile import _is_included_in_docker_context


def _archive(member_name: str, payload: bytes, *, symlink: bool = False) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo(member_name)
        if symlink:
            member.type = tarfile.SYMTYPE
            member.linkname = "/private/sentinel"
            member.size = 0
        else:
            member.mode = 0o755
            member.size = len(payload)
        archive.addfile(member, None if symlink else io.BytesIO(payload))
    return output.getvalue()


def _lock(age: bytes, minisign: bytes) -> dict[str, object]:
    return {
        "schema_version": "sen-qa-backup-tools/v1",
        "age": {
            "version": "1.3.1",
            "archive_url": (
                "https://github.com/FiloSottile/age/releases/download/"
                "v1.3.1/age-v1.3.1-linux-amd64.tar.gz"
            ),
            "archive_sha256": hashlib.sha256(age).hexdigest(),
            "archive_size": len(age),
            "binary_path": "age/age",
        },
        "minisign": {
            "version": "0.12",
            "archive_url": (
                "https://github.com/jedisct1/minisign/releases/download/"
                "0.12/minisign-0.12-linux.tar.gz"
            ),
            "archive_sha256": hashlib.sha256(minisign).hexdigest(),
            "archive_size": len(minisign),
            "binary_path": "minisign-linux/x86_64/minisign",
        },
    }


class _Response(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def test_prepare_backup_tools_verifies_and_installs_exact_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the backup image installing an unverified host or archive binary."""
    age = _archive("age/age", b"age-binary")
    minisign = _archive("minisign-linux/x86_64/minisign", b"minisign-binary")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(_lock(age, minisign)), encoding="utf-8")
    by_url = {
        _lock(age, minisign)["age"]["archive_url"]: age,
        _lock(age, minisign)["minisign"]["archive_url"]: minisign,
    }

    def open_url(url: str, *, timeout: int) -> _Response:
        assert timeout == 30
        return _Response(by_url[url])

    monkeypatch.setattr(prepare.urllib.request, "urlopen", open_url)
    output = tmp_path / "output"

    prepare.install_backup_tools(lock_path, output)

    assert (output / "age").read_bytes() == b"age-binary"
    assert (output / "minisign").read_bytes() == b"minisign-binary"
    assert stat.S_IMODE((output / "age").stat().st_mode) == 0o555
    assert stat.S_IMODE((output / "minisign").stat().st_mode) == 0o555


def test_prepare_backup_tools_rejects_symlink_member_and_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches archive traversal and downloaded-byte substitution."""
    age = _archive("age/age", b"", symlink=True)
    minisign = _archive("minisign-linux/x86_64/minisign", b"minisign-binary")
    lock = _lock(age, minisign)
    lock["minisign"]["archive_sha256"] = "0" * 64
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    by_url = {
        lock["age"]["archive_url"]: age,
        lock["minisign"]["archive_url"]: minisign,
    }
    monkeypatch.setattr(
        prepare.urllib.request,
        "urlopen",
        lambda url, *, timeout: _Response(by_url[url]),
    )

    with pytest.raises(prepare.BackupToolPreparationError):
        prepare.install_backup_tools(lock_path, tmp_path / "output")

    assert not (tmp_path / "output" / "age").exists()
    assert not (tmp_path / "output" / "minisign").exists()


def test_backup_image_is_pinned_nonroot_and_context_complete() -> None:
    """Catches mutable/runtime downloads or a deny-by-default context omission."""
    instructions = Path("docker/backup.Dockerfile").read_text(encoding="utf-8")
    from_lines = [
        line for line in instructions.splitlines() if line.startswith("FROM ")
    ]

    assert len(from_lines) == 2
    assert all(
        line.startswith("FROM --platform=linux/amd64 ") and "@sha256:" in line
        for line in from_lines
    )
    assert "COPY --from=builder /opt/backup-tools/age" in instructions
    assert "COPY --from=builder /opt/backup-tools/minisign" in instructions
    assert "USER 65532:65532" in instructions
    runtime = instructions.split(" AS runtime\n", maxsplit=1)[1]
    assert "prepare_backup_tools.py" not in runtime
    assert "backup-tools.lock.json" not in runtime
    for required in (
        "config/backup-tools.lock.json",
        "docker/backup.Dockerfile",
        "docker/prepare_backup_tools.py",
    ):
        assert _is_included_in_docker_context(required)
