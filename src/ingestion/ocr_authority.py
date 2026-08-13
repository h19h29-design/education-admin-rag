"""Fail-closed per-year authority lock for the approved OCR corpus."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, TypeAlias

_PUBLIC_ERROR: Final = "OCR authority lock is invalid"
_LOCK_SCHEMA: Final = "sen-qa-ocr-authority-lock/v1"
_ENTRY_KEYS: Final = frozenset(
    {
        "authority",
        "doc_id",
        "engine",
        "record_schema",
        "source_sha256",
        "year",
    }
)
_ROOT_KEYS: Final = frozenset({"entries", "schema_version", "self_sha256"})
_MAX_LOCK_BYTES: Final = 65_536
_SHA256_HEX_LENGTH: Final = 64
_CONCRETE_PATH_TYPE: Final = type(Path())
_PADDLE_CONTAINER_AUTHORITY: Final = (
    "ghcr.io/h19h29-design/education-admin-rag-ingestion@sha256:"
    "1b13f568237b23bbe858bef1bac1ef7081094554f3d3ba5750c4dae72feec9d6"
)
_SOURCE_BINDINGS: Final = {
    2023: (
        "sen-qa-2023",
        "9a6a5b3745eb4200c70f9d33395c8b25b5a55fa171036127f2be5791224455bc",
        "sen-qa-ocr-page/v2",
        "paddleocr",
    ),
    2024: (
        "sen-qa-2024",
        "fc1494eff8ee3fe9b53606dd5f55468d8ec254b9d2d661fba6c5e4b46daa99ed",
        "sen-qa-ocr-page/v3",
        "apple-vision",
    ),
    2025: (
        "sen-qa-2025",
        "9a1a7b0ebf1346b540c97d9990dd3b43c647ce397322ff0fabe6d2de84c0ce03",
        "sen-qa-ocr-page/v3",
        "apple-vision",
    ),
}
_YEARS: Final = (2023, 2024, 2025)
JsonObject: TypeAlias = dict[str, object]


class OcrAuthorityLockError(Exception):
    """A deliberately value-free OCR authority boundary failure."""


@dataclass(frozen=True, slots=True)
class OcrAuthorityEntry:
    """One exact source, schema, engine, and execution-authority binding."""

    year: int
    doc_id: str
    source_sha256: str
    record_schema: str
    engine: str
    authority: str


@dataclass(frozen=True, slots=True)
class OcrAuthorityLock:
    """The complete immutable authority set for OCR years 2023 through 2025."""

    schema_version: str
    entries: tuple[OcrAuthorityEntry, ...]
    self_sha256: str


def _raise_invalid() -> NoReturn:
    raise OcrAuthorityLockError(_PUBLIC_ERROR)


def _is_sha256_hex(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == _SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_runtime_fingerprint(value: object) -> bool:
    return (
        type(value) is str
        and value.startswith("sha256:")
        and _is_sha256_hex(value.removeprefix("sha256:"))
    )


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


def _entry_payload(entry: OcrAuthorityEntry) -> JsonObject:
    return {
        "authority": entry.authority,
        "doc_id": entry.doc_id,
        "engine": entry.engine,
        "record_schema": entry.record_schema,
        "source_sha256": entry.source_sha256,
        "year": entry.year,
    }


def _body_payload(entries: tuple[OcrAuthorityEntry, ...]) -> JsonObject:
    return {
        "entries": [_entry_payload(entry) for entry in entries],
        "schema_version": _LOCK_SCHEMA,
    }


def _self_sha256(entries: tuple[OcrAuthorityEntry, ...]) -> str:
    return hashlib.sha256(_canonical_json(_body_payload(entries))).hexdigest()


def _entry_is_valid(entry: object, expected_year: int) -> bool:
    if type(entry) is not OcrAuthorityEntry:
        return False
    expected_doc_id, expected_source, expected_schema, expected_engine = (
        _SOURCE_BINDINGS[expected_year]
    )
    if (
        type(entry.year) is not int
        or entry.year != expected_year
        or type(entry.doc_id) is not str
        or entry.doc_id != expected_doc_id
        or type(entry.source_sha256) is not str
        or entry.source_sha256 != expected_source
        or type(entry.record_schema) is not str
        or entry.record_schema != expected_schema
        or type(entry.engine) is not str
        or entry.engine != expected_engine
        or type(entry.authority) is not str
    ):
        return False
    if expected_year == 2023:
        return entry.authority == _PADDLE_CONTAINER_AUTHORITY
    return _is_runtime_fingerprint(entry.authority)


def _lock_is_valid(lock: object) -> bool:
    return (
        type(lock) is OcrAuthorityLock
        and type(lock.schema_version) is str
        and lock.schema_version == _LOCK_SCHEMA
        and type(lock.entries) is tuple
        and len(lock.entries) == len(_YEARS)
        and all(
            _entry_is_valid(entry, year)
            for entry, year in zip(lock.entries, _YEARS, strict=True)
        )
        and type(lock.self_sha256) is str
        and _is_sha256_hex(lock.self_sha256)
        and hmac.compare_digest(lock.self_sha256, _self_sha256(lock.entries))
    )


def build_ocr_authority_lock(
    *,
    vision_2024_runtime_fingerprint: str,
    vision_2025_runtime_fingerprint: str,
) -> OcrAuthorityLock:
    """Build the one canonical lock shape using separately attested Vision runs."""

    if not _is_runtime_fingerprint(
        vision_2024_runtime_fingerprint
    ) or not _is_runtime_fingerprint(vision_2025_runtime_fingerprint):
        _raise_invalid()
    entries = (
        OcrAuthorityEntry(
            year=2023,
            doc_id=_SOURCE_BINDINGS[2023][0],
            source_sha256=_SOURCE_BINDINGS[2023][1],
            record_schema=_SOURCE_BINDINGS[2023][2],
            engine=_SOURCE_BINDINGS[2023][3],
            authority=_PADDLE_CONTAINER_AUTHORITY,
        ),
        OcrAuthorityEntry(
            year=2024,
            doc_id=_SOURCE_BINDINGS[2024][0],
            source_sha256=_SOURCE_BINDINGS[2024][1],
            record_schema=_SOURCE_BINDINGS[2024][2],
            engine=_SOURCE_BINDINGS[2024][3],
            authority=vision_2024_runtime_fingerprint,
        ),
        OcrAuthorityEntry(
            year=2025,
            doc_id=_SOURCE_BINDINGS[2025][0],
            source_sha256=_SOURCE_BINDINGS[2025][1],
            record_schema=_SOURCE_BINDINGS[2025][2],
            engine=_SOURCE_BINDINGS[2025][3],
            authority=vision_2025_runtime_fingerprint,
        ),
    )
    return OcrAuthorityLock(
        schema_version=_LOCK_SCHEMA,
        entries=entries,
        self_sha256=_self_sha256(entries),
    )


def canonical_ocr_authority_bytes(lock: OcrAuthorityLock) -> bytes:
    """Serialize a fully revalidated lock into its only accepted byte form."""

    if not _lock_is_valid(lock):
        _raise_invalid()
    payload = _body_payload(lock.entries)
    payload["self_sha256"] = lock.self_sha256
    return _canonical_json(payload)


def _read_bounded_regular_file(path: Path) -> bytes | None:
    descriptor: int | None = None
    raw: bytes | None = None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before_link = os.lstat(path)
        if stat.S_ISLNK(before_link.st_mode):
            return None
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_LOCK_BYTES
        ):
            return None
        chunks: list[bytes] = []
        remaining = _MAX_LOCK_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        candidate = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            len(candidate) > _MAX_LOCK_BYTES
            or len(candidate) != before.st_size
            or identity_before != identity_after
        ):
            return None
        raw = candidate
    except (OSError, OverflowError, ValueError):
        raw = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raw = None
    return raw


def _decode_unique_json_object(raw: bytes) -> JsonObject | None:
    duplicate_key = False
    invalid_constant = False

    def unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
        nonlocal duplicate_key
        output: JsonObject = {}
        for key, value in pairs:
            if key in output:
                duplicate_key = True
            output[key] = value
        return output

    def reject_constant(_: str) -> None:
        nonlocal invalid_constant
        invalid_constant = True

    payload: object | None
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        payload = None
    if duplicate_key or invalid_constant or type(payload) is not dict:
        return None
    return payload


def _entry_from_payload(
    payload: object, expected_year: int
) -> OcrAuthorityEntry | None:
    if type(payload) is not dict or set(payload) != _ENTRY_KEYS:
        return None
    year = payload["year"]
    doc_id = payload["doc_id"]
    source_sha256 = payload["source_sha256"]
    record_schema = payload["record_schema"]
    engine = payload["engine"]
    authority = payload["authority"]
    if (
        type(year) is not int
        or type(doc_id) is not str
        or type(source_sha256) is not str
        or type(record_schema) is not str
        or type(engine) is not str
        or type(authority) is not str
    ):
        return None
    entry = OcrAuthorityEntry(
        year=year,
        doc_id=doc_id,
        source_sha256=source_sha256,
        record_schema=record_schema,
        engine=engine,
        authority=authority,
    )
    return entry if _entry_is_valid(entry, expected_year) else None


def _lock_from_payload(payload: JsonObject) -> OcrAuthorityLock | None:
    if set(payload) != _ROOT_KEYS:
        return None
    schema_version = payload["schema_version"]
    serialized_entries = payload["entries"]
    claimed_self_sha256 = payload["self_sha256"]
    if (
        type(schema_version) is not str
        or schema_version != _LOCK_SCHEMA
        or type(serialized_entries) is not list
        or len(serialized_entries) != len(_YEARS)
        or type(claimed_self_sha256) is not str
        or not _is_sha256_hex(claimed_self_sha256)
    ):
        return None
    parsed_entries: list[OcrAuthorityEntry] = []
    for serialized_entry, year in zip(serialized_entries, _YEARS, strict=True):
        entry = _entry_from_payload(serialized_entry, year)
        if entry is None:
            return None
        parsed_entries.append(entry)
    entries = tuple(parsed_entries)
    expected_self_sha256 = _self_sha256(entries)
    if not hmac.compare_digest(claimed_self_sha256, expected_self_sha256):
        return None
    lock = OcrAuthorityLock(
        schema_version=schema_version,
        entries=entries,
        self_sha256=claimed_self_sha256,
    )
    return lock if _lock_is_valid(lock) else None


def load_ocr_authority_lock(path: Path, *, expected_sha256: str) -> OcrAuthorityLock:
    """Load a canonical bounded file under an independently supplied SHA-256."""

    if type(path) is not _CONCRETE_PATH_TYPE or not _is_sha256_hex(expected_sha256):
        _raise_invalid()
    raw = _read_bounded_regular_file(path)
    if raw is None or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_sha256
    ):
        _raise_invalid()
    payload = _decode_unique_json_object(raw)
    if payload is None:
        _raise_invalid()
    lock = _lock_from_payload(payload)
    if lock is None:
        _raise_invalid()
    canonical = canonical_ocr_authority_bytes(lock)
    if not hmac.compare_digest(raw, canonical):
        _raise_invalid()
    return lock
