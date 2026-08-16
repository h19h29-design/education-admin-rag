#!/usr/bin/env python3
"""Install the read-only SEN-QA preview search skill into a configured profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import NoReturn

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_TEMPLATE_BYTES = 128 * 1024
_MAX_ATTESTATION_BYTES = 1024 * 1024
_MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024


class InstallError(ValueError):
    pass


def _raise() -> NoReturn:
    raise InstallError("install_failed") from None


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _read_regular(path: Path, *, maximum: int) -> bytes:
    descriptor: int | None = None
    failed = False
    data = b""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            failed = True
        else:
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if len(data) > maximum or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                failed = True
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        _raise()
    return data


def _load_json(raw: bytes) -> dict[str, object]:
    value: object = None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        pass
    if type(value) is not dict:
        _raise()
    return value


def _require_absolute(path: Path) -> None:
    if not path.is_absolute():
        _raise()


def _ensure_owner_directory(path: Path) -> None:
    _require_absolute(path)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError:
                _raise()
            continue
        except OSError:
            _raise()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            _raise()


def _preflight_target(path: Path, expected: bytes) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _raise()
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        _raise()
    if _read_regular(path, maximum=max(len(expected), 1)) != expected:
        _raise()
    return True


def _write_new(path: Path, data: bytes, *, mode: int) -> None:
    descriptor: int | None = None
    failed = False
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            mode,
        )
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                failed = True
                break
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                failed = True
    if failed:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        _raise()


def _validate_authority(
    database: Path, attestation: Path, expected_attestation_sha256: str
) -> None:
    if _SHA256_RE.fullmatch(expected_attestation_sha256) is None:
        _raise()
    attestation_bytes = _read_regular(attestation, maximum=_MAX_ATTESTATION_BYTES)
    if hashlib.sha256(attestation_bytes).hexdigest() != expected_attestation_sha256:
        _raise()
    payload = _load_json(attestation_bytes)
    database_sha256 = payload.get("preview_db_sha256")
    if (
        payload.get("schema_version") != "sen-qa-preview-rag-attestation/v2"
        or payload.get("warning_code") != "unreviewed_incomplete_preview"
        or payload.get("production_eligible") is not False
        or payload.get("complete_corpus") is not False
        or type(database_sha256) is not str
        or _SHA256_RE.fullmatch(database_sha256) is None
    ):
        _raise()
    database_bytes = _read_regular(database, maximum=_MAX_DATABASE_BYTES)
    if hashlib.sha256(database_bytes).hexdigest() != database_sha256:
        _raise()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--bin-root", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--search-source", type=Path, required=True)
    parser.add_argument("--skill-template", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--expected-attestation-sha256", required=True)
    return parser


def _install(arguments: argparse.Namespace) -> None:
    paths = (
        arguments.profile_root,
        arguments.bin_root,
        arguments.config_root,
        arguments.search_source,
        arguments.skill_template,
        arguments.database,
        arguments.attestation,
    )
    for path in paths:
        _require_absolute(path)
    _validate_authority(
        arguments.database,
        arguments.attestation,
        arguments.expected_attestation_sha256,
    )
    search_bytes = _read_regular(arguments.search_source, maximum=_MAX_SOURCE_BYTES)
    template_bytes = _read_regular(
        arguments.skill_template, maximum=_MAX_TEMPLATE_BYTES
    )
    try:
        template = template_bytes.decode("utf-8")
    except UnicodeError:
        _raise()

    search_target = arguments.bin_root / "senqa-preview-search"
    config_directory = arguments.config_root / "senqa-preview-rag"
    config_target = config_directory / "config.json"
    skill_directory = arguments.profile_root / "skills" / "sen-qa-preview-rag"
    skill_target = skill_directory / "SKILL.md"
    config_bytes = _canonical_json(
        {
            "attestation": str(arguments.attestation),
            "database": str(arguments.database),
            "expected_attestation_sha256": arguments.expected_attestation_sha256,
            "schema_version": "sen-qa-preview-search-config/v1",
        }
    )
    skill_bytes = (
        template.replace("{{SEARCH_COMMAND}}", str(search_target))
        .replace("{{CONFIG_PATH}}", str(config_target))
        .encode("utf-8")
    )
    if b"{{" in skill_bytes or b"}}" in skill_bytes:
        _raise()

    _ensure_owner_directory(arguments.bin_root)
    _ensure_owner_directory(config_directory)
    _ensure_owner_directory(skill_directory)
    targets = (
        (search_target, search_bytes, 0o500),
        (config_target, config_bytes, 0o600),
        (skill_target, skill_bytes, 0o600),
    )
    existing = tuple(_preflight_target(path, data) for path, data, _ in targets)
    created: list[Path] = []
    try:
        for is_existing, (path, data, mode) in zip(existing, targets, strict=True):
            if is_existing:
                os.chmod(path, mode, follow_symlinks=False)
            else:
                _write_new(path, data, mode=mode)
                created.append(path)
    except (InstallError, OSError):
        for path in reversed(created):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        _raise()


def main() -> int:
    try:
        arguments = _parser().parse_args()
        _install(arguments)
    except (InstallError, OSError, ValueError, TypeError):
        print("install_failed", file=sys.stderr)
        return 2
    print("installed=1 profile=configured skill=sen-qa-preview-rag")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
