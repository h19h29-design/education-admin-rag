"""Download and install only byte-pinned backup tools during image build."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn, cast

_MAX_LOCK_BYTES = 64 * 1024
_MAX_ARCHIVE_BYTES = 50_000_000
_TOOL_KEYS = {
    "archive_sha256",
    "archive_size",
    "archive_url",
    "binary_path",
    "version",
}


class BackupToolPreparationError(RuntimeError):
    """A fixed, value-free backup-tool preparation failure."""


def _raise() -> NoReturn:
    raise BackupToolPreparationError("backup_tool_preparation_failed") from None


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _read_lock(path: Path) -> dict[str, dict[str, object]] | None:
    try:
        payload = path.read_bytes()
        decoded = json.loads(payload, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, _DuplicateKey):
        return None
    if (
        not 1 <= len(payload) <= _MAX_LOCK_BYTES
        or type(decoded) is not dict
        or set(decoded) != {"age", "minisign", "schema_version"}
        or decoded.get("schema_version") != "sen-qa-backup-tools/v1"
    ):
        return None
    checked: dict[str, dict[str, object]] = {}
    for name in ("age", "minisign"):
        raw = decoded.get(name)
        if type(raw) is not dict or set(raw) != _TOOL_KEYS:
            return None
        tool = cast(dict[str, object], raw)
        version = tool.get("version")
        url = tool.get("archive_url")
        digest = tool.get("archive_sha256")
        size = tool.get("archive_size")
        binary_path = tool.get("binary_path")
        if (
            type(version) is not str
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,2}", version) is None
            or type(url) is not str
            or type(digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or type(size) is not int
            or not 1 <= size <= _MAX_ARCHIVE_BYTES
            or type(binary_path) is not str
        ):
            return None
        expected_url = (
            "https://github.com/FiloSottile/age/releases/download/"
            f"v{version}/age-v{version}-linux-amd64.tar.gz"
            if name == "age"
            else "https://github.com/jedisct1/minisign/releases/download/"
            f"{version}/minisign-{version}-linux.tar.gz"
        )
        expected_member = (
            "age/age" if name == "age" else "minisign-linux/x86_64/minisign"
        )
        if url != expected_url or binary_path != expected_member:
            return None
        checked[name] = tool
    return checked


def _download(tool: dict[str, object]) -> bytes | None:
    expected_size = cast(int, tool["archive_size"])
    try:
        with urllib.request.urlopen(
            cast(str, tool["archive_url"]), timeout=30
        ) as response:
            payload: bytes = response.read(expected_size + 1)
            trailing: bytes = response.read(1)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if (
        trailing
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != tool["archive_sha256"]
    ):
        return None
    return payload


def _member_bytes(archive_bytes: bytes, expected_name: str) -> bytes | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = archive.getmembers()
            matches = [member for member in members if member.name == expected_name]
            if len(members) > 1_000 or len(matches) != 1:
                return None
            member = matches[0]
            if not member.isfile() or not 1 <= member.size <= _MAX_ARCHIVE_BYTES:
                return None
            handle = archive.extractfile(member)
            if handle is None:
                return None
            payload = handle.read(member.size + 1)
            if len(payload) != member.size:
                return None
            return payload
    except (OSError, tarfile.TarError, ValueError):
        return None


def install_backup_tools(lock_path: Path, output: Path) -> None:
    """Install verified age/minisign binaries atomically into a new directory."""
    if not isinstance(lock_path, Path) or not isinstance(output, Path):
        _raise()
    lock = _read_lock(lock_path)
    if lock is None or output.exists() or output.is_symlink():
        _raise()
    try:
        output.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    except OSError:
        _raise()
    failed = False
    try:
        for tool_name in ("age", "minisign"):
            tool = lock[tool_name]
            archive_bytes = _download(tool)
            binary = (
                _member_bytes(archive_bytes, cast(str, tool["binary_path"]))
                if archive_bytes is not None
                else None
            )
            if binary is None:
                failed = True
                break
            target = temporary / tool_name
            with target.open("xb") as handle:
                handle.write(binary)
                handle.flush()
                os.fsync(handle.fileno())
            target.chmod(0o555)
        if not failed:
            os.replace(temporary, output)
    except (OSError, TypeError, ValueError):
        failed = True
    if failed:
        try:
            shutil.rmtree(temporary)
        except OSError:
            pass
        _raise()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    install_backup_tools(arguments.lock, arguments.output)


if __name__ == "__main__":
    main()
