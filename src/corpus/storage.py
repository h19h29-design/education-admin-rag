"""Fail-closed canonical SQLite storage and deterministic JSONL export."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn, Self, cast

from pydantic import BaseModel

from src.corpus.chunking import (
    RoleSource,
    TokenizerContract,
    VerifiedChunkSet,
    revalidate_verified_chunk_set,
    role_source_manifest_bytes,
)
from src.corpus.ids import validate_case_id
from src.corpus.models import (
    Case,
    Document,
    DocumentPageCounts,
    IngestionRun,
    SourceSpan,
)
from src.corpus.relations import (
    VerifiedCaseRelation,
    VerifiedLawRef,
    canonical_case_sha256,
    revalidate_verified_law_ref,
    revalidate_verified_relation,
)
from src.ingestion.review import (
    CanonicalReviewRegistry,
    VerifiedCanonicalReviewRegistry,
    validate_review_reason,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_RE = re.compile(r"^corpus-[0-9]{14}-[0-9a-f]{8}$")
_ACTOR_RE = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_OS_ACTOR_RE = re.compile(r"^uid:([0-9]+):")
_SNAPSHOT_SCHEMA = "review-decision-snapshot-v1"
_ISSUANCE_SCHEMA = "sen-qa-issued-case-authority/v1"
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_RECORDS = 100_000
_MAX_DATABASE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_EXPORT_BYTES = 512 * 1024 * 1024
_MAX_SOURCE_FILENAME_BYTES = 512
_EXPORT_TABLES = (
    "build_meta",
    "documents",
    "issued_case_ids",
    "cases",
    "source_spans",
    "chunks",
    "chunk_source_spans",
    "law_refs",
    "case_relations",
    "corrections",
    "review_events",
    "ingestion_runs",
    "tokenizer_contract",
    "review_registry",
    "review_registry_locations",
    "case_authorities",
)
_CONTENT_EXPORTS = (
    "case_authorities.jsonl",
    "case_relations.jsonl",
    "cases.jsonl",
    "chunk_source_spans.jsonl",
    "chunks.jsonl",
    "corrections.jsonl",
    "documents.jsonl",
    "issued_case_ids.jsonl",
    "law_refs.jsonl",
    "review_events.jsonl",
    "review_registry.jsonl",
    "review_registry_locations.jsonl",
    "source_spans.jsonl",
    "tokenizer_contract.jsonl",
)


class StorageError(ValueError):
    """A fixed, value-free canonical storage boundary failure."""


def _raise(message: str) -> NoReturn:
    raise StorageError(message) from None


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _valid_release(value: object) -> bool:
    return isinstance(value, str) and _RELEASE_RE.fullmatch(value) is not None


def _valid_case_id(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_case_id(value)
    except ValueError:
        return False
    return True


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_content_sha256(export_hashes: object) -> str:
    """Hash exact semantic exports while excluding run and physical DB state."""
    if type(export_hashes) is not dict:
        _raise("canonical content export is incomplete")
    approved = cast(dict[object, object], export_hashes)
    if set(_CONTENT_EXPORTS) - set(approved) or any(
        not _valid_sha256(approved[name]) for name in _CONTENT_EXPORTS
    ):
        _raise("canonical content export is incomplete")
    payload = {
        "schema_version": "sen-qa-canonical-content/v1",
        "tables": {name: approved[name] for name in _CONTENT_EXPORTS},
    }
    return hashlib.sha256(
        b"sen-qa-canonical-content-v1\0" + _canonical_json(payload)
    ).hexdigest()


def _valid_source_filename(value: object) -> bool:
    return (
        isinstance(value, str)
        and value not in {".", ".."}
        and unicodedata.normalize("NFC", value) == value
        and 1 <= len(value.encode("utf-8")) <= _MAX_SOURCE_FILENAME_BYTES
        and "/" not in value
        and "\\" not in value
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        and Path(value).name == value
    )


def _valid_canonical_utc(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return (
        parsed.tzinfo is not None
        and parsed.utcoffset() == UTC.utcoffset(parsed)
        and parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") == value
    )


def _same_reviewer(first: str | None, second: str | None) -> bool:
    if first is None or second is None:
        return False
    if first == second:
        return True
    first_uid = _OS_ACTOR_RE.match(first)
    second_uid = _OS_ACTOR_RE.match(second)
    return (
        first_uid is not None
        and second_uid is not None
        and int(first_uid.group(1)) == int(second_uid.group(1))
    )


def _directory_path_from_fd(descriptor: int) -> Path:
    if hasattr(fcntl, "F_GETPATH"):
        raw = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
        encoded = raw.split(b"\0", 1)[0]
        if not encoded or len(encoded) >= 1023:
            _raise("storage path is invalid")
        return Path(encoded.decode())
    proc_path = Path(f"/proc/self/fd/{descriptor}")
    return Path(os.readlink(proc_path))


@contextmanager
def _open_parent_directory(path: Path) -> Iterator[tuple[int, str, Path]]:
    absolute = Path(os.path.abspath(path))
    leaf = absolute.name
    if leaf in {"", ".", ".."}:
        _raise("storage path is invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(absolute.anchor, flags)
        for component in absolute.parent.parts[1:]:
            next_descriptor = os.open(
                component,
                flags | nofollow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor, leaf, _directory_path_from_fd(descriptor)
    except (OSError, UnicodeError, ValueError):
        _raise("storage path is invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _leaf_details(directory_fd: int, leaf: str) -> os.stat_result | None:
    try:
        return os.stat(leaf, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        _raise("storage path is invalid")


def _require_regular_database(path: Path, *, max_bytes: int) -> Path:
    with _open_parent_directory(path) as (directory_fd, leaf, current_parent):
        details = _leaf_details(directory_fd, leaf)
        if (
            details is None
            or not stat.S_ISREG(details.st_mode)
            or details.st_size <= 0
            or details.st_size > max_bytes
            or details.st_nlink != 1
        ):
            _raise("storage database is invalid")
        return current_parent / leaf


def _regular_file_sha256(path: Path, *, max_bytes: int) -> str:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
            or before.st_nlink != 1
        ):
            _raise("storage database is invalid")
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _raise("storage database is invalid")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or os.read(descriptor, 1):
            _raise("storage database is invalid")
        return digest.hexdigest()
    except OSError:
        _raise("storage database is invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stable_bundle_file_at(
    directory_fd: int,
    leaf: str,
    *,
    max_bytes: int,
    capture_bytes: bool = False,
) -> tuple[str, int, bytes | None]:
    descriptor = -1
    try:
        descriptor = os.open(
            leaf,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > max_bytes
            or before.st_nlink != 1
        ):
            _raise("published bundle is invalid")
        digest = hashlib.sha256()
        captured = bytearray() if capture_bytes else None
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                _raise("published bundle is invalid")
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
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
        ) or os.read(descriptor, 1):
            _raise("published bundle is invalid")
        return (
            digest.hexdigest(),
            before.st_size,
            bytes(captured) if captured is not None else None,
        )
    except OSError:
        _raise("published bundle is invalid")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sqlite_uri(path: Path, *, mode: str) -> str:
    return f"{path.as_uri()}?mode={mode}&nofollow=1"


def _publish_file_noreplace(
    *, directory_fd: int, temporary_leaf: str, final_leaf: str
) -> None:
    try:
        os.link(
            temporary_leaf,
            final_leaf,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary_leaf, dir_fd=directory_fd)
    except OSError:
        _raise("storage publication failed")


@dataclass(frozen=True, slots=True)
class ReviewDecisionCase:
    case_id: str
    promotion_envelope_sha256: str
    corrections: tuple[dict[str, object], ...]
    review_record: dict[str, object]
    events: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True, init=False)
class VerifiedReviewDecisionSnapshot:
    canonical_bytes: bytes
    fingerprint_sha256: str
    registry_fingerprint_sha256: str
    cases: tuple[ReviewDecisionCase, ...]


@dataclass(frozen=True, slots=True, init=False)
class VerifiedPromotionEnvelope:
    canonical_bytes: bytes
    fingerprint_sha256: str
    candidate_case: Case
    role_sources: tuple[RoleSource, ...]
    corrections: tuple[dict[str, object], ...]
    parser_authority_sha256: str
    raw_authority_sha256: str


def load_promotion_envelope(
    raw: bytes, *, expected_sha256: str
) -> VerifiedPromotionEnvelope:
    """Load a reviewed candidate/raw-to-canonical bridge under an external digest."""
    if (
        type(raw) is not bytes
        or len(raw) > _MAX_SNAPSHOT_BYTES
        or not _valid_sha256(expected_sha256)
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)
    ):
        _raise("promotion envelope is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateKey):
        payload = None
    if type(payload) is not dict or set(payload) != {
        "candidate_case",
        "corrections",
        "parser_authority_sha256",
        "raw_authority_sha256",
        "role_sources",
        "schema_version",
    }:
        _raise("promotion envelope is invalid")
    payload = cast(dict[str, object], payload)
    if (
        payload["schema_version"] != "sen-qa-promotion-envelope/v1"
        or not _valid_sha256(payload["parser_authority_sha256"])
        or not _valid_sha256(payload["raw_authority_sha256"])
        or type(payload["candidate_case"]) is not dict
        or type(payload["corrections"]) is not list
        or type(payload["role_sources"]) is not list
        or len(cast(list[object], payload["role_sources"])) > _MAX_RECORDS
    ):
        _raise("promotion envelope is invalid")
    try:
        candidate_case = Case.model_validate_json(
            _canonical_json(payload["candidate_case"])
        )
    except (TypeError, ValueError):
        candidate_case = None
    if (
        candidate_case is None
        or not _valid_case_id(candidate_case.case_id)
        or candidate_case.review_status not in {"machine_extracted", "needs_review"}
        or candidate_case.critical_field_review != "pending"
        or candidate_case.search_eligible
        or candidate_case.answer_eligible
    ):
        _raise("promotion envelope is invalid")
    checked_corrections = _parse_corrections(
        payload["corrections"],
        case_id=candidate_case.case_id,
    )
    if checked_corrections is None:
        _raise("promotion envelope is invalid")
    last_correction_by_field = {
        cast(str, item["target_field"]): item for item in checked_corrections
    }
    for field_name, correction in last_correction_by_field.items():
        corrected_value = getattr(candidate_case, field_name)
        if (
            not isinstance(corrected_value, str)
            or hashlib.sha256(corrected_value.encode("utf-8")).hexdigest()
            != correction["after_sha256"]
        ):
            _raise("promotion envelope is invalid")
    role_sources: list[RoleSource] = []
    role_keys = {
        "raw_text",
        "role",
        "source_span_index",
        "table_evidence_sha256",
        "table_header",
        "table_header_raw_text",
        "table_header_source_span_index",
        "text",
    }
    for item in cast(list[object], payload["role_sources"]):
        if type(item) is not dict or set(item) != role_keys:
            _raise("promotion envelope is invalid")
        item = cast(dict[str, object], item)
        try:
            role_sources.append(
                RoleSource(
                    role=cast(Any, item["role"]),
                    text=cast(Any, item["text"]),
                    raw_text=cast(Any, item["raw_text"]),
                    source_span_index=cast(Any, item["source_span_index"]),
                    table_header=cast(Any, item["table_header"]),
                    table_header_raw_text=cast(Any, item["table_header_raw_text"]),
                    table_header_source_span_index=cast(
                        Any, item["table_header_source_span_index"]
                    ),
                    table_evidence_sha256=cast(Any, item["table_evidence_sha256"]),
                )
            )
        except (TypeError, ValueError):
            _raise("promotion envelope is invalid")
    try:
        role_source_manifest_bytes(candidate_case, tuple(role_sources))
    except ValueError:
        _raise("promotion envelope is invalid")
    if _canonical_json(payload) + b"\n" != raw:
        _raise("promotion envelope is not canonical")
    verified = object.__new__(VerifiedPromotionEnvelope)
    object.__setattr__(verified, "canonical_bytes", raw)
    object.__setattr__(verified, "fingerprint_sha256", expected_sha256)
    object.__setattr__(verified, "candidate_case", candidate_case)
    object.__setattr__(verified, "role_sources", tuple(role_sources))
    object.__setattr__(verified, "corrections", checked_corrections)
    object.__setattr__(
        verified,
        "parser_authority_sha256",
        payload["parser_authority_sha256"],
    )
    object.__setattr__(
        verified,
        "raw_authority_sha256",
        payload["raw_authority_sha256"],
    )
    return verified


_RECORD_KEYS = {
    "answer_eligible",
    "answer_reviewer_id",
    "basis_verified",
    "content_sha256",
    "content_verified",
    "critical_field_review",
    "critical_reviewer_id",
    "privacy_verified",
    "review_status",
    "search_eligible",
    "search_reviewer_id",
    "version",
}
_EVENT_KEYS = {
    "action",
    "actor_id",
    "after_state",
    "batch_manifest_sha256",
    "before_state",
    "event_id",
    "event_sequence",
    "occurred_at",
    "reason",
    "reviewed_content_sha256",
}
_CORRECTION_KEYS = {
    "after_sha256",
    "before_sha256",
    "case_id",
    "corrected_at",
    "correction_id",
    "reason_code",
    "reviewer_id",
    "sequence",
    "target_field",
}
_CORRECTION_FIELDS = {
    "title_raw",
    "title_normalized",
    "question",
    "answer",
    "facts",
    "basis_text",
}


def _review_reason_is_valid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        validate_review_reason(value)
    except ValueError:
        return False
    return True


def _parse_corrections(
    raw: object,
    *,
    case_id: str,
) -> tuple[dict[str, object], ...] | None:
    if type(raw) is not list or len(raw) > _MAX_RECORDS:
        return None
    checked: list[dict[str, object]] = []
    sequence_by_field: dict[str, int] = {}
    previous_after_by_field: dict[str, str] = {}
    for item in raw:
        if type(item) is not dict or set(item) != _CORRECTION_KEYS:
            return None
        correction = cast(dict[str, object], item)
        target_field = correction.get("target_field")
        sequence = correction.get("sequence")
        if (
            correction.get("case_id") != case_id
            or not isinstance(target_field, str)
            or target_field not in _CORRECTION_FIELDS
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != sequence_by_field.get(target_field, 0) + 1
            or not _valid_sha256(correction.get("before_sha256"))
            or not _valid_sha256(correction.get("after_sha256"))
            or correction.get("before_sha256") == correction.get("after_sha256")
            or not isinstance(correction.get("reviewer_id"), str)
            or _ACTOR_RE.fullmatch(cast(str, correction["reviewer_id"])) is None
            or not isinstance(correction.get("reason_code"), str)
            or _ACTOR_RE.fullmatch(cast(str, correction["reason_code"])) is None
            or not isinstance(correction.get("corrected_at"), str)
            or not _valid_canonical_utc(correction.get("corrected_at"))
            or not isinstance(correction.get("correction_id"), str)
        ):
            return None
        if sequence > 1 and previous_after_by_field[target_field] != correction.get(
            "before_sha256"
        ):
            return None
        identity_payload = {
            key: correction[key] for key in _CORRECTION_KEYS if key != "correction_id"
        }
        expected_id = (
            "correction-"
            + hashlib.sha256(
                b"sen-qa-correction-envelope-v1\0" + _canonical_json(identity_payload)
            ).hexdigest()[:32]
        )
        if correction["correction_id"] != expected_id:
            return None
        sequence_by_field[target_field] = sequence
        previous_after_by_field[target_field] = cast(str, correction["after_sha256"])
        checked.append(dict(correction))
    return tuple(checked)


def _parse_review_case(raw: object) -> ReviewDecisionCase | None:
    if type(raw) is not dict or set(raw) != {
        "case_id",
        "corrections",
        "events",
        "promotion_envelope_sha256",
        "review_record",
    }:
        return None
    raw = cast(dict[str, object], raw)
    case_id = raw["case_id"]
    promotion = raw["promotion_envelope_sha256"]
    record = raw["review_record"]
    corrections = raw["corrections"]
    events = raw["events"]
    if (
        not _valid_case_id(case_id)
        or not _valid_sha256(promotion)
        or type(record) is not dict
        or set(record) != _RECORD_KEYS
        or type(events) is not list
        or type(corrections) is not list
        or not 1 <= len(events) <= 10_000
    ):
        return None
    record = cast(dict[str, object], record)
    status = record.get("review_status")
    if (
        record.get("content_sha256") != promotion
        or status not in {"search_approved", "approved", "rejected"}
        or record.get("critical_field_review") not in {"pending", "verified"}
        or type(record.get("search_eligible")) is not bool
        or type(record.get("answer_eligible")) is not bool
        or type(record.get("version")) is not int
        or cast(int, record["version"]) != len(events)
        or any(
            type(record.get(key)) is not bool
            for key in ("content_verified", "basis_verified", "privacy_verified")
        )
    ):
        return None
    checked_events: list[dict[str, object]] = []
    seen_event_ids: set[str] = set()
    prior_timestamp: str | None = None
    for sequence, event in enumerate(events, start=1):
        if type(event) is not dict or set(event) != _EVENT_KEYS:
            return None
        event = cast(dict[str, object], event)
        event_id = event.get("event_id")
        occurred_at = event.get("occurred_at")
        if (
            isinstance(event.get("event_sequence"), bool)
            or type(event.get("event_sequence")) is not int
            or event.get("event_sequence") != sequence
            or not isinstance(event_id, str)
            or not 1 <= len(event_id.encode("utf-8")) <= 512
            or event_id in seen_event_ids
            or not isinstance(event.get("action"), str)
            or not isinstance(event.get("actor_id"), str)
            or _ACTOR_RE.fullmatch(cast(str, event["actor_id"])) is None
            or not _valid_canonical_utc(occurred_at)
            or (
                prior_timestamp is not None and cast(str, occurred_at) < prior_timestamp
            )
            or event.get("reviewed_content_sha256") != promotion
            or not _review_reason_is_valid(event.get("reason"))
            or (
                event.get("batch_manifest_sha256") is not None
                and not _valid_sha256(event.get("batch_manifest_sha256"))
            )
        ):
            return None
        seen_event_ids.add(event_id)
        prior_timestamp = cast(str, occurred_at)
        checked_events.append(dict(event))

    state = "machine_extracted"
    actors: dict[str, str] = {}
    allowed = {
        ("machine_extracted", "enqueue", "needs_review"),
        ("needs_review", "verify_fields", "needs_review"),
        ("needs_review", "approve_search", "search_approved"),
        ("search_approved", "approve_answer", "approved"),
        ("machine_extracted", "reject", "rejected"),
        ("needs_review", "reject", "rejected"),
        ("search_approved", "reject", "rejected"),
    }
    for event in checked_events:
        transition = (state, event["action"], event["after_state"])
        if event["before_state"] != state or transition not in allowed:
            return None
        action = cast(str, event["action"])
        if action in actors:
            return None
        actors[action] = cast(str, event["actor_id"])
        state = cast(str, event["after_state"])
    if state != status or checked_events[0]["action"] not in {"enqueue", "reject"}:
        return None

    critical_actor = actors.get("verify_fields")
    search_actor = actors.get("approve_search")
    answer_actor = actors.get("approve_answer")
    if (
        record.get("critical_field_review")
        != ("verified" if critical_actor is not None else "pending")
        or record.get("critical_reviewer_id") != critical_actor
        or record.get("search_reviewer_id") != search_actor
        or record.get("answer_reviewer_id") != answer_actor
    ):
        return None
    if status == "search_approved" and (
        record.get("search_eligible") is not True
        or record.get("answer_eligible") is not False
        or any(
            record.get(key) is not False
            for key in ("content_verified", "basis_verified", "privacy_verified")
        )
    ):
        return None
    if status == "approved" and (
        record.get("search_eligible") is not True
        or record.get("answer_eligible") is not True
        or any(
            record.get(key) is not True
            for key in ("content_verified", "basis_verified", "privacy_verified")
        )
        or answer_actor is None
        or _same_reviewer(answer_actor, critical_actor)
        or _same_reviewer(answer_actor, search_actor)
    ):
        return None
    if status == "rejected" and (
        record.get("search_eligible") is not False
        or record.get("answer_eligible") is not False
        or any(
            record.get(key) is not False
            for key in ("content_verified", "basis_verified", "privacy_verified")
        )
    ):
        return None
    checked_corrections = _parse_corrections(
        corrections,
        case_id=cast(str, case_id),
    )
    if checked_corrections is None:
        return None
    return ReviewDecisionCase(
        case_id=cast(str, case_id),
        promotion_envelope_sha256=cast(str, promotion),
        corrections=checked_corrections,
        review_record=dict(record),
        events=tuple(checked_events),
    )


def load_review_decision_snapshot(
    raw: bytes, *, expected_sha256: str
) -> VerifiedReviewDecisionSnapshot:
    """Load canonical decision bytes only after an independently supplied digest pin."""
    if (
        type(raw) is not bytes
        or len(raw) > _MAX_SNAPSHOT_BYTES
        or not _valid_sha256(expected_sha256)
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)
    ):
        _raise("review decision snapshot is invalid")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, _DuplicateKey):
        payload = None
    if type(payload) is not dict or set(payload) != {
        "cases",
        "registry_fingerprint_sha256",
        "schema_version",
    }:
        _raise("review decision snapshot is invalid")
    payload = cast(dict[str, object], payload)
    raw_cases = payload["cases"]
    registry = payload["registry_fingerprint_sha256"]
    if (
        payload["schema_version"] != _SNAPSHOT_SCHEMA
        or not _valid_sha256(registry)
        or type(raw_cases) is not list
        or not 1 <= len(raw_cases) <= _MAX_RECORDS
    ):
        _raise("review decision snapshot is invalid")
    cases = tuple(_parse_review_case(item) for item in raw_cases)
    if any(case is None for case in cases):
        _raise("review decision snapshot is invalid")
    approved_cases = cast(tuple[ReviewDecisionCase, ...], cases)
    case_ids = tuple(case.case_id for case in approved_cases)
    if case_ids != tuple(sorted(case_ids)) or len(case_ids) != len(set(case_ids)):
        _raise("review decision snapshot is invalid")
    if _canonical_json(payload) + b"\n" != raw:
        _raise("review decision snapshot is not canonical")
    verified = object.__new__(VerifiedReviewDecisionSnapshot)
    object.__setattr__(verified, "canonical_bytes", raw)
    object.__setattr__(verified, "fingerprint_sha256", expected_sha256)
    object.__setattr__(verified, "registry_fingerprint_sha256", registry)
    object.__setattr__(verified, "cases", approved_cases)
    return verified


@dataclass(frozen=True, slots=True)
class IssuedCaseRecord:
    case_id: str
    state: str
    first_release_id: str
    first_content_sha256: str
    current_content_sha256: str
    retired_release_id: str | None


@dataclass(frozen=True, slots=True)
class IssuanceHead:
    generation: int
    release_id: str | None
    bundle_sha256: str | None
    authority_sha256: str


@dataclass(frozen=True, slots=True, init=False)
class StorageProjectionReceipt:
    """Capability binding one published canonical DB to an issuance projection."""

    release_id: str
    predecessor_generation: int
    predecessor_authority_sha256: str
    predecessor_bundle_sha256: str | None
    database_sha256: str
    bundle_sha256: str
    projection_sha256: str
    records: tuple[IssuedCaseRecord, ...]
    _lease_token: object


@dataclass(frozen=True, slots=True)
class IssuedReleaseRecord:
    generation: int
    release_id: str
    predecessor_bundle_sha256: str | None
    bundle_sha256: str
    projection_sha256: str
    predecessor_authority_sha256: str
    authority_sha256: str


def _projection_bytes(records: tuple[IssuedCaseRecord, ...]) -> bytes:
    return (
        _canonical_json(
            [
                {
                    "case_id": item.case_id,
                    "current_content_sha256": item.current_content_sha256,
                    "first_content_sha256": item.first_content_sha256,
                    "first_release_id": item.first_release_id,
                    "retired_release_id": item.retired_release_id,
                    "state": item.state,
                }
                for item in records
            ]
        )
        + b"\n"
    )


def _projection_sha256(records: tuple[IssuedCaseRecord, ...]) -> str:
    return hashlib.sha256(_projection_bytes(records)).hexdigest()


def _release_authority_sha256(
    *,
    generation: int,
    release_id: str,
    predecessor_bundle_sha256: str | None,
    bundle_sha256: str,
    projection_sha256: str,
    predecessor_authority_sha256: str,
) -> str:
    payload = {
        "bundle_sha256": bundle_sha256,
        "generation": generation,
        "predecessor_authority_sha256": predecessor_authority_sha256,
        "predecessor_bundle_sha256": predecessor_bundle_sha256,
        "projection_sha256": projection_sha256,
        "release_id": release_id,
        "schema_version": "sen-qa-issued-release/v1",
    }
    return hashlib.sha256(
        b"sen-qa-issued-release-authority-v1\0" + _canonical_json(payload)
    ).hexdigest()


def _authority_bytes(
    generation: int,
    release_id: str | None,
    bundle_sha256: str | None,
    records: tuple[IssuedCaseRecord, ...],
    releases: tuple[IssuedReleaseRecord, ...] = (),
) -> bytes:
    payload = {
        "bundle_sha256": bundle_sha256,
        "generation": generation,
        "records": [
            {
                "case_id": item.case_id,
                "first_content_sha256": item.first_content_sha256,
                "first_release_id": item.first_release_id,
                "current_content_sha256": item.current_content_sha256,
                "retired_release_id": item.retired_release_id,
                "state": item.state,
            }
            for item in records
        ],
        "release_id": release_id,
        "releases": [
            {
                "bundle_sha256": item.bundle_sha256,
                "generation": item.generation,
                "predecessor_authority_sha256": item.predecessor_authority_sha256,
                "predecessor_bundle_sha256": item.predecessor_bundle_sha256,
                "projection_sha256": item.projection_sha256,
                "release_id": item.release_id,
            }
            for item in releases
        ],
        "schema_version": _ISSUANCE_SCHEMA,
    }
    return _canonical_json(payload) + b"\n"


GENESIS_ISSUANCE_AUTHORITY_SHA256 = hashlib.sha256(
    _authority_bytes(0, None, None, ())
).hexdigest()


_ISSUANCE_SCHEMA_SQL = """
CREATE TABLE issuance_meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    schema_version TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>=0),
    release_id TEXT,
    bundle_sha256 TEXT,
    authority_sha256 TEXT NOT NULL
) STRICT;
CREATE TABLE issued_case_ids (
    case_id TEXT PRIMARY KEY,
    state TEXT NOT NULL CHECK(state IN ('active','retired')),
    first_release_id TEXT NOT NULL,
    first_content_sha256 TEXT NOT NULL,
    current_content_sha256 TEXT NOT NULL,
    retired_release_id TEXT
) STRICT;
CREATE TABLE issued_releases (
    generation INTEGER PRIMARY KEY CHECK(generation>=1),
    release_id TEXT NOT NULL UNIQUE,
    predecessor_bundle_sha256 TEXT,
    bundle_sha256 TEXT NOT NULL UNIQUE,
    projection_sha256 TEXT NOT NULL,
    predecessor_authority_sha256 TEXT NOT NULL,
    authority_sha256 TEXT NOT NULL UNIQUE
) STRICT;
"""

_ISSUANCE_TRIGGER_SQL = """
CREATE TRIGGER guard_issuance_meta_insert BEFORE INSERT ON issuance_meta
WHEN issuance_write_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issuance_meta_update BEFORE UPDATE ON issuance_meta
WHEN issuance_write_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issuance_meta_delete BEFORE DELETE ON issuance_meta BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_case_insert BEFORE INSERT ON issued_case_ids
WHEN issuance_write_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_case_update BEFORE UPDATE ON issued_case_ids
WHEN issuance_write_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_case_delete BEFORE DELETE ON issued_case_ids BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_release_insert BEFORE INSERT ON issued_releases
WHEN issuance_write_authorized() != 1 BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_release_update BEFORE UPDATE ON issued_releases BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
CREATE TRIGGER guard_issued_release_delete BEFORE DELETE ON issued_releases BEGIN
    SELECT RAISE(ABORT, 'issuance authority is immutable');
END;
"""

_ISSUANCE_TRIGGER_NAMES = {
    "guard_issuance_meta_delete",
    "guard_issuance_meta_insert",
    "guard_issuance_meta_update",
    "guard_issued_case_delete",
    "guard_issued_case_insert",
    "guard_issued_case_update",
    "guard_issued_release_delete",
    "guard_issued_release_insert",
    "guard_issued_release_update",
}


def _install_issuance_authorizer(
    connection: sqlite3.Connection, state: dict[str, bool]
) -> None:
    connection.create_function(
        "issuance_write_authorized",
        0,
        lambda: int(state["authorized"]),
    )


def _issuance_schema_identity(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (
            cast(str, row[0]),
            cast(str, row[1]),
            cast(str, row[2]),
            " ".join(cast(str, row[3]).split()),
        )
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


@lru_cache(maxsize=1)
def _expected_issuance_schema_identity() -> tuple[tuple[str, str, str, str], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_ISSUANCE_SCHEMA_SQL + _ISSUANCE_TRIGGER_SQL)
        return _issuance_schema_identity(connection)
    finally:
        connection.close()


def _validate_issuance_schema(connection: sqlite3.Connection) -> None:
    tables = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    triggers = {
        cast(str, row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    if (
        tables != {"issuance_meta", "issued_case_ids", "issued_releases"}
        or triggers != _ISSUANCE_TRIGGER_NAMES
        or _issuance_schema_identity(connection) != _expected_issuance_schema_identity()
    ):
        _raise("issuance registry schema is invalid")


def initialize_issuance_registry(path: Path, *, expected_genesis_sha256: str) -> None:
    """Create the one persistent cross-release ID authority at its fixed genesis."""
    if expected_genesis_sha256 != GENESIS_ISSUANCE_AUTHORITY_SHA256:
        _raise("issuance registry genesis is invalid")
    failed = False
    with _open_parent_directory(path) as (directory_fd, leaf, current_parent):
        if _leaf_details(directory_fd, leaf) is not None:
            _raise("issuance registry genesis is invalid")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{leaf}.tmp-", dir=current_parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        connection: sqlite3.Connection | None = None
        state = {"authorized": True}
        try:
            connection = sqlite3.connect(temporary, isolation_level=None)
            _install_issuance_authorizer(connection, state)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_ISSUANCE_SCHEMA_SQL + _ISSUANCE_TRIGGER_SQL)
            connection.execute(
                "INSERT INTO issuance_meta VALUES(1,?,?,?,?,?)",
                (
                    _ISSUANCE_SCHEMA,
                    0,
                    None,
                    None,
                    GENESIS_ISSUANCE_AUTHORITY_SHA256,
                ),
            )
            state["authorized"] = False
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            connection = None
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            _publish_file_noreplace(
                directory_fd=directory_fd,
                temporary_leaf=temporary.name,
                final_leaf=leaf,
            )
            os.fsync(directory_fd)
        except (sqlite3.Error, OSError, StorageError):
            failed = True
        finally:
            if connection is not None:
                connection.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(temporary.name + suffix, dir_fd=directory_fd)
                except OSError:
                    pass
    if failed:
        _raise("cannot initialize issuance registry")


def _issuance_records(connection: sqlite3.Connection) -> tuple[IssuedCaseRecord, ...]:
    rows = connection.execute(
        "SELECT case_id,state,first_release_id,first_content_sha256,"
        "current_content_sha256,retired_release_id "
        "FROM issued_case_ids ORDER BY case_id"
    ).fetchall()
    if len(rows) > _MAX_RECORDS:
        _raise("issuance registry is invalid")
    records: list[IssuedCaseRecord] = []
    for row in rows:
        try:
            record = IssuedCaseRecord(*row)
        except (TypeError, ValueError):
            _raise("issuance registry is invalid")
        if (
            not _valid_case_id(record.case_id)
            or record.state not in {"active", "retired"}
            or not _valid_release(record.first_release_id)
            or not _valid_sha256(record.first_content_sha256)
            or not _valid_sha256(record.current_content_sha256)
            or (record.state == "active") != (record.retired_release_id is None)
            or (
                record.retired_release_id is not None
                and not _valid_release(record.retired_release_id)
            )
        ):
            _raise("issuance registry is invalid")
        records.append(record)
    return tuple(records)


def _issuance_releases(
    connection: sqlite3.Connection,
) -> tuple[IssuedReleaseRecord, ...]:
    rows = connection.execute(
        "SELECT generation,release_id,predecessor_bundle_sha256,bundle_sha256,"
        "projection_sha256,predecessor_authority_sha256,authority_sha256 "
        "FROM issued_releases ORDER BY generation"
    ).fetchall()
    if len(rows) > _MAX_RECORDS:
        _raise("issuance registry is invalid")
    releases: list[IssuedReleaseRecord] = []
    for row in rows:
        try:
            release = IssuedReleaseRecord(*row)
        except (TypeError, ValueError):
            _raise("issuance registry is invalid")
        if (
            isinstance(release.generation, bool)
            or not isinstance(release.generation, int)
            or release.generation < 1
            or not _valid_release(release.release_id)
            or (
                release.predecessor_bundle_sha256 is not None
                and not _valid_sha256(release.predecessor_bundle_sha256)
            )
            or not _valid_sha256(release.bundle_sha256)
            or not _valid_sha256(release.projection_sha256)
            or not _valid_sha256(release.predecessor_authority_sha256)
            or not _valid_sha256(release.authority_sha256)
        ):
            _raise("issuance registry is invalid")
        releases.append(release)
    return tuple(releases)


def _read_head(connection: sqlite3.Connection) -> IssuanceHead:
    _validate_issuance_schema(connection)
    row = connection.execute(
        "SELECT schema_version,generation,release_id,bundle_sha256,authority_sha256 "
        "FROM issuance_meta WHERE singleton=1"
    ).fetchone()
    if row is None:
        _raise("issuance registry is invalid")
    schema, generation, release_id, bundle_sha256, authority_sha256 = row
    if (
        schema != _ISSUANCE_SCHEMA
        or type(generation) is not int
        or generation < 0
        or (release_id is None) != (bundle_sha256 is None)
        or (release_id is not None and not _valid_release(release_id))
        or (bundle_sha256 is not None and not _valid_sha256(bundle_sha256))
        or not _valid_sha256(authority_sha256)
    ):
        _raise("issuance registry is invalid")
    records = _issuance_records(connection)
    releases = _issuance_releases(connection)
    if generation == 0:
        if (
            records
            or releases
            or release_id is not None
            or bundle_sha256 is not None
            or authority_sha256 != GENESIS_ISSUANCE_AUTHORITY_SHA256
        ):
            _raise("issuance registry authority mismatch")
    else:
        if len(releases) != generation:
            _raise("issuance registry authority mismatch")
        prior_authority = GENESIS_ISSUANCE_AUTHORITY_SHA256
        prior_bundle: str | None = None
        seen_release_ids: set[str] = set()
        seen_bundles: set[str] = set()
        for expected_generation, issued_release in enumerate(releases, start=1):
            expected_authority = _release_authority_sha256(
                generation=expected_generation,
                release_id=issued_release.release_id,
                predecessor_bundle_sha256=prior_bundle,
                bundle_sha256=issued_release.bundle_sha256,
                projection_sha256=issued_release.projection_sha256,
                predecessor_authority_sha256=prior_authority,
            )
            if (
                issued_release.generation != expected_generation
                or issued_release.release_id in seen_release_ids
                or issued_release.bundle_sha256 in seen_bundles
                or issued_release.predecessor_bundle_sha256 != prior_bundle
                or issued_release.predecessor_authority_sha256 != prior_authority
                or not hmac.compare_digest(
                    issued_release.authority_sha256,
                    expected_authority,
                )
            ):
                _raise("issuance registry authority mismatch")
            seen_release_ids.add(issued_release.release_id)
            seen_bundles.add(issued_release.bundle_sha256)
            prior_authority = issued_release.authority_sha256
            prior_bundle = issued_release.bundle_sha256
        latest = releases[-1]
        if (
            latest.release_id != release_id
            or latest.bundle_sha256 != bundle_sha256
            or latest.authority_sha256 != authority_sha256
            or latest.projection_sha256 != _projection_sha256(records)
        ):
            _raise("issuance registry authority mismatch")
    return IssuanceHead(generation, release_id, bundle_sha256, authority_sha256)


def read_issuance_head(path: Path) -> IssuanceHead:
    stable_path = _require_regular_database(path, max_bytes=_MAX_DATABASE_BYTES)
    connection: sqlite3.Connection | None = None
    failed = False
    try:
        connection = sqlite3.connect(_sqlite_uri(stable_path, mode="ro"), uri=True)
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            _raise("issuance registry is invalid")
        return _read_head(connection)
    except sqlite3.Error:
        failed = True
    finally:
        if connection is not None:
            connection.close()
    if failed:
        _raise("cannot read issuance registry")
    _raise("cannot read issuance registry")


class IssuanceLease:
    """BEGIN IMMEDIATE lease held until a full bundle is published or abandoned."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        head: IssuanceHead,
        authorization_state: dict[str, bool],
    ) -> None:
        self._connection = connection
        self.head = head
        self._authorization_state = authorization_state
        self._closed = False
        self._committed = False
        self._receipt_issued = False
        self._receipt_token = object()
        self._issued_receipt: StorageProjectionReceipt | None = None
        self._commit_receipt: StorageProjectionReceipt | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._connection.in_transaction:
            self._connection.execute("ROLLBACK")
        self._connection.close()
        self._closed = True

    def projected_records(
        self, cases: tuple[Case, ...], release_id: str
    ) -> tuple[IssuedCaseRecord, ...]:
        if self._closed or not _valid_release(release_id):
            _raise("issuance lease is invalid")
        if any(
            release.release_id == release_id
            for release in _issuance_releases(self._connection)
        ):
            _raise("issuance release is already present")
        prior = {
            record.case_id: record for record in _issuance_records(self._connection)
        }
        current = {case.case_id: case for case in cases}
        if len(current) != len(cases) or any(
            case_id in prior and prior[case_id].state == "retired"
            for case_id in current
        ):
            _raise("retired case ID cannot be reactivated")
        result: list[IssuedCaseRecord] = []
        for case_id, record in prior.items():
            if record.state == "active" and case_id not in current:
                record = IssuedCaseRecord(
                    record.case_id,
                    "retired",
                    record.first_release_id,
                    record.first_content_sha256,
                    record.current_content_sha256,
                    release_id,
                )
            elif record.state == "active":
                record = IssuedCaseRecord(
                    record.case_id,
                    record.state,
                    record.first_release_id,
                    record.first_content_sha256,
                    canonical_case_sha256(current[case_id]),
                    None,
                )
            result.append(record)
        for case_id, case in current.items():
            if case_id not in prior:
                content_sha256 = canonical_case_sha256(case)
                result.append(
                    IssuedCaseRecord(
                        case_id,
                        "active",
                        release_id,
                        content_sha256,
                        content_sha256,
                        None,
                    )
                )
        return tuple(sorted(result, key=lambda item: item.case_id))

    def _issue_projection_receipt(
        self,
        *,
        release_id: str,
        bundle_sha256: str,
        records: tuple[IssuedCaseRecord, ...],
    ) -> StorageProjectionReceipt:
        if (
            self._closed
            or self._committed
            or self._receipt_issued
            or not _valid_release(release_id)
            or not _valid_sha256(bundle_sha256)
        ):
            _raise("issuance projection receipt is invalid")
        checked_records = tuple(records)
        if checked_records != tuple(
            sorted(checked_records, key=lambda item: item.case_id)
        ) or len({item.case_id for item in checked_records}) != len(checked_records):
            _raise("issuance projection receipt is invalid")
        receipt = object.__new__(StorageProjectionReceipt)
        object.__setattr__(receipt, "release_id", release_id)
        object.__setattr__(receipt, "predecessor_generation", self.head.generation)
        object.__setattr__(
            receipt,
            "predecessor_authority_sha256",
            self.head.authority_sha256,
        )
        object.__setattr__(
            receipt,
            "predecessor_bundle_sha256",
            self.head.bundle_sha256,
        )
        object.__setattr__(receipt, "bundle_sha256", bundle_sha256)
        object.__setattr__(receipt, "database_sha256", bundle_sha256)
        object.__setattr__(
            receipt,
            "projection_sha256",
            _projection_sha256(checked_records),
        )
        object.__setattr__(receipt, "records", checked_records)
        object.__setattr__(receipt, "_lease_token", self._receipt_token)
        self._receipt_issued = True
        self._issued_receipt = receipt
        return receipt

    def bind_published_bundle(
        self,
        receipt: StorageProjectionReceipt,
        *,
        bundle_path: Path,
    ) -> StorageProjectionReceipt:
        """Bind the storage projection capability to one complete disk bundle."""
        if (
            self._closed
            or self._committed
            or receipt is not self._issued_receipt
            or self._commit_receipt is not None
            or not _valid_sha256(receipt.database_sha256)
            or receipt.projection_sha256 != _projection_sha256(receipt.records)
            or not isinstance(bundle_path, Path)
        ):
            _raise("published bundle receipt is invalid")
        root_fd = -1
        jsonl_fd = -1
        database_sha256 = ""
        manifest: bytes | None = None
        actual_exports: dict[str, str] = {}
        try:
            with _open_parent_directory(bundle_path) as (parent_fd, leaf, _parent):
                details = _leaf_details(parent_fd, leaf)
                if details is None or not stat.S_ISDIR(details.st_mode):
                    _raise("published bundle receipt is invalid")
                root_fd = os.open(
                    leaf,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
            if set(os.listdir(root_fd)) != {
                "canonical.sqlite3",
                "jsonl",
                "manifest.json",
            }:
                _raise("published bundle receipt is invalid")
            database_sha256, _, _ = _stable_bundle_file_at(
                root_fd,
                "canonical.sqlite3",
                max_bytes=_MAX_DATABASE_BYTES,
            )
            _, _, manifest = _stable_bundle_file_at(
                root_fd,
                "manifest.json",
                max_bytes=_MAX_SNAPSHOT_BYTES,
                capture_bytes=True,
            )
            jsonl_fd = os.open(
                "jsonl",
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            expected_exports = {f"{table}.jsonl" for table in _EXPORT_TABLES}
            if set(os.listdir(jsonl_fd)) != expected_exports:
                _raise("published bundle receipt is invalid")
            total_export_bytes = 0
            for name in sorted(expected_exports):
                digest, size, _ = _stable_bundle_file_at(
                    jsonl_fd,
                    name,
                    max_bytes=_MAX_EXPORT_BYTES,
                )
                total_export_bytes += size
                if total_export_bytes > _MAX_EXPORT_BYTES:
                    _raise("published bundle receipt is invalid")
                actual_exports[name] = digest
        except (OSError, StorageError):
            _raise("published bundle receipt is invalid")
        finally:
            if jsonl_fd >= 0:
                os.close(jsonl_fd)
            if root_fd >= 0:
                os.close(root_fd)
        if manifest is None:
            _raise("published bundle receipt is invalid")
        manifest_bytes = manifest
        try:
            payload = json.loads(
                manifest_bytes.decode("ascii"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, _DuplicateKey):
            payload = None
        if type(payload) is not dict or set(payload) != {
            "canonical_content_sha256",
            "database_sha256",
            "exports",
            "predecessor_bundle_sha256",
            "projection_sha256",
            "release_id",
            "schema_version",
        }:
            _raise("published bundle receipt is invalid")
        payload = cast(dict[str, object], payload)
        exports = payload["exports"]
        if (
            payload["schema_version"] != "sen-qa-canonical-bundle/v1"
            or payload["release_id"] != receipt.release_id
            or payload["database_sha256"] != receipt.database_sha256
            or payload["database_sha256"] != database_sha256
            or payload["projection_sha256"] != receipt.projection_sha256
            or payload["predecessor_bundle_sha256"] != receipt.predecessor_bundle_sha256
            or not _valid_sha256(payload["canonical_content_sha256"])
            or type(exports) is not dict
            or set(cast(dict[object, object], exports))
            != {f"{table}.jsonl" for table in _EXPORT_TABLES}
            or any(
                not isinstance(name, str)
                or not name.endswith(".jsonl")
                or not _valid_sha256(digest)
                for name, digest in cast(dict[object, object], exports).items()
            )
            or exports != actual_exports
            or payload["canonical_content_sha256"] != canonical_content_sha256(exports)
            or _canonical_json(payload) + b"\n" != manifest_bytes
        ):
            _raise("published bundle receipt is invalid")
        bundle_sha256 = hashlib.sha256(
            b"sen-qa-canonical-bundle-v1\0" + manifest_bytes
        ).hexdigest()
        bound = object.__new__(StorageProjectionReceipt)
        for field_name in (
            "release_id",
            "predecessor_generation",
            "predecessor_authority_sha256",
            "predecessor_bundle_sha256",
            "database_sha256",
            "projection_sha256",
            "records",
            "_lease_token",
        ):
            object.__setattr__(bound, field_name, getattr(receipt, field_name))
        object.__setattr__(bound, "bundle_sha256", bundle_sha256)
        self._commit_receipt = bound
        return bound

    def commit_published_bundle(
        self,
        *,
        receipt: StorageProjectionReceipt,
    ) -> IssuanceHead:
        """Advance the durable head only after Build verified a complete published bundle."""
        if (
            self._closed
            or self._committed
            or type(receipt) is not StorageProjectionReceipt
            or receipt is not self._commit_receipt
            or receipt._lease_token is not self._receipt_token
            or receipt.predecessor_generation != self.head.generation
            or receipt.predecessor_authority_sha256 != self.head.authority_sha256
            or receipt.predecessor_bundle_sha256 != self.head.bundle_sha256
            or not _valid_release(receipt.release_id)
            or not _valid_sha256(receipt.bundle_sha256)
            or not _valid_sha256(receipt.database_sha256)
            or not _valid_sha256(receipt.projection_sha256)
            or receipt.projection_sha256 != _projection_sha256(receipt.records)
        ):
            _raise("issuance lease commit is invalid")
        records = receipt.records
        prior = {
            record.case_id: record for record in _issuance_records(self._connection)
        }
        self._authorization_state["authorized"] = True
        try:
            if set(prior) - {item.case_id for item in records}:
                _raise("issuance projection is invalid")
            for record in records:
                previous = prior.get(record.case_id)
                if previous is None:
                    self._connection.execute(
                        "INSERT INTO issued_case_ids VALUES(?,?,?,?,?,?)",
                        (
                            record.case_id,
                            record.state,
                            record.first_release_id,
                            record.first_content_sha256,
                            record.current_content_sha256,
                            record.retired_release_id,
                        ),
                    )
                elif previous.state == "retired":
                    if previous != record:
                        _raise("issuance projection is invalid")
                elif record.state == "active":
                    if (
                        previous.first_release_id != record.first_release_id
                        or previous.first_content_sha256 != record.first_content_sha256
                        or record.retired_release_id is not None
                    ):
                        _raise("issuance projection is invalid")
                    self._connection.execute(
                        "UPDATE issued_case_ids SET current_content_sha256=? "
                        "WHERE case_id=? AND state='active'",
                        (record.current_content_sha256, record.case_id),
                    )
                elif (
                    record.first_release_id != previous.first_release_id
                    or record.first_content_sha256 != previous.first_content_sha256
                    or record.current_content_sha256 != previous.current_content_sha256
                    or record.retired_release_id != receipt.release_id
                ):
                    _raise("issuance projection is invalid")
                else:
                    self._connection.execute(
                        "UPDATE issued_case_ids SET state='retired',retired_release_id=? "
                        "WHERE case_id=? AND state='active' AND retired_release_id IS NULL",
                        (record.retired_release_id, record.case_id),
                    )
            generation = self.head.generation + 1
            authority = _release_authority_sha256(
                generation=generation,
                release_id=receipt.release_id,
                predecessor_bundle_sha256=self.head.bundle_sha256,
                bundle_sha256=receipt.bundle_sha256,
                projection_sha256=receipt.projection_sha256,
                predecessor_authority_sha256=self.head.authority_sha256,
            )
            self._connection.execute(
                "INSERT INTO issued_releases VALUES(?,?,?,?,?,?,?)",
                (
                    generation,
                    receipt.release_id,
                    self.head.bundle_sha256,
                    receipt.bundle_sha256,
                    receipt.projection_sha256,
                    self.head.authority_sha256,
                    authority,
                ),
            )
            self._connection.execute(
                "UPDATE issuance_meta SET generation=?,release_id=?,bundle_sha256=?,"
                "authority_sha256=? WHERE singleton=1",
                (generation, receipt.release_id, receipt.bundle_sha256, authority),
            )
            self._connection.execute("COMMIT")
        finally:
            self._authorization_state["authorized"] = False
        self._committed = True
        self.head = IssuanceHead(
            generation,
            receipt.release_id,
            receipt.bundle_sha256,
            authority,
        )
        return self.head


def acquire_issuance_lease(
    path: Path,
    *,
    expected_generation: int,
    expected_authority_sha256: str,
    expected_predecessor_bundle_sha256: str | None,
) -> IssuanceLease:
    """Acquire a serialized persistent-head CAS lease."""
    stable_path = _require_regular_database(path, max_bytes=_MAX_DATABASE_BYTES)
    connection: sqlite3.Connection | None = None
    state = {"authorized": False}
    failed = False
    try:
        connection = sqlite3.connect(
            _sqlite_uri(stable_path, mode="rw"),
            isolation_level=None,
            timeout=5.0,
            uri=True,
        )
        _install_issuance_authorizer(connection, state)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        head = _read_head(connection)
        if (
            isinstance(expected_generation, bool)
            or not isinstance(expected_generation, int)
            or head.generation != expected_generation
            or not _valid_sha256(expected_authority_sha256)
            or not hmac.compare_digest(head.authority_sha256, expected_authority_sha256)
            or head.bundle_sha256 != expected_predecessor_bundle_sha256
        ):
            _raise("issuance predecessor does not match the persistent head")
        return IssuanceLease(connection, head, state)
    except (sqlite3.Error, StorageError):
        failed = True
        if connection is not None and connection.in_transaction:
            connection.execute("ROLLBACK")
        if connection is not None:
            connection.close()
    if failed:
        _raise("issuance predecessor does not match the persistent head")
    _raise("issuance predecessor does not match the persistent head")


@dataclass(frozen=True, slots=True)
class CanonicalStorageBatch:
    release_id: str
    documents: tuple[Document, ...]
    cases: tuple[Case, ...]
    chunk_sets: tuple[VerifiedChunkSet, ...]
    law_refs: tuple[VerifiedLawRef, ...]
    relations: tuple[VerifiedCaseRelation, ...]
    relation_approval_sha256s: dict[str, str]
    ingestion_runs: tuple[IngestionRun, ...]
    tokenizer_contract: TokenizerContract
    promotion_envelopes: tuple[VerifiedPromotionEnvelope, ...]
    review_registry: VerifiedCanonicalReviewRegistry
    review_decision_snapshot: VerifiedReviewDecisionSnapshot


def _fields(value: object, model: type[BaseModel]) -> dict[str, object] | None:
    if type(value) is not model:
        return None
    try:
        raw = object.__getattribute__(value, "__dict__")
    except (AttributeError, TypeError):
        return None
    if type(raw) is not dict or set(raw) != set(model.model_fields):
        return None
    return cast(dict[str, object], raw.copy())


def _revalidate_span(value: object) -> SourceSpan | None:
    raw = _fields(value, SourceSpan)
    if raw is None:
        return None
    try:
        return SourceSpan.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _revalidate_case(value: object) -> Case | None:
    raw = _fields(value, Case)
    if raw is None or type(raw.get("source_spans")) is not tuple:
        return None
    spans = tuple(
        _revalidate_span(item) for item in cast(tuple[object, ...], raw["source_spans"])
    )
    if any(span is None for span in spans):
        return None
    raw["source_spans"] = tuple(
        cast(SourceSpan, span).model_dump(mode="python") for span in spans
    )
    try:
        case = Case.model_validate(raw)
    except (TypeError, ValueError):
        return None
    return case if _valid_case_id(case.case_id) else None


def _revalidate_simple(value: object, model: type[BaseModel]) -> BaseModel | None:
    raw = _fields(value, model)
    if raw is None:
        return None
    try:
        return model.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _revalidate_run(value: object) -> IngestionRun | None:
    raw = _fields(value, IngestionRun)
    if raw is None or type(raw.get("document_page_counts")) is not dict:
        return None
    counts: dict[str, object] = {}
    for key, item in cast(dict[object, object], raw["document_page_counts"]).items():
        checked = _revalidate_simple(item, DocumentPageCounts)
        if not isinstance(key, str) or checked is None:
            return None
        counts[key] = checked.model_dump(mode="python")
    raw["document_page_counts"] = counts
    try:
        return IngestionRun.model_validate(raw)
    except (TypeError, ValueError):
        return None


def _revalidate_contract(value: object) -> TokenizerContract | None:
    if type(value) is not TokenizerContract:
        return None
    if (
        value.model_name != "BAAI/bge-m3"
        or not isinstance(value.revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", value.revision) is None
        or not _valid_sha256(value.model_lock_sha256)
        or not _valid_sha256(value.runtime_fingerprint_sha256)
    ):
        return None
    return TokenizerContract(
        value.model_name,
        value.revision,
        value.model_lock_sha256,
        value.runtime_fingerprint_sha256,
    )


def _model_json(model: BaseModel) -> str:
    return _canonical_json(model.model_dump(mode="json")).decode("utf-8")


_CANONICAL_SCHEMA_SQL = """
CREATE TABLE build_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),release_id TEXT NOT NULL,predecessor_bundle_sha256 TEXT,review_snapshot_sha256 TEXT NOT NULL,registry_sha256 TEXT NOT NULL) STRICT;
CREATE TABLE documents(doc_id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,pdf_page_count INTEGER NOT NULL CHECK(pdf_page_count>0),payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.doc_id')=doc_id)) STRICT;
CREATE TABLE issued_case_ids(case_id TEXT PRIMARY KEY,state TEXT NOT NULL CHECK(state IN ('active','retired')),first_release_id TEXT NOT NULL,first_content_sha256 TEXT NOT NULL,current_content_sha256 TEXT NOT NULL,retired_release_id TEXT) STRICT;
CREATE TABLE cases(case_id TEXT PRIMARY KEY,doc_id TEXT NOT NULL,content_sha256 TEXT NOT NULL,review_status TEXT NOT NULL CHECK(review_status IN ('search_approved','approved','rejected')),search_eligible INTEGER NOT NULL CHECK(search_eligible IN (0,1)),answer_eligible INTEGER NOT NULL CHECK(answer_eligible IN (0,1)),payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.case_id')=case_id AND json_extract(payload_json,'$.doc_id')=doc_id AND json_extract(payload_json,'$.review_status')=review_status AND json_extract(payload_json,'$.search_eligible')=search_eligible AND json_extract(payload_json,'$.answer_eligible')=answer_eligible),FOREIGN KEY(doc_id) REFERENCES documents(doc_id),FOREIGN KEY(case_id) REFERENCES issued_case_ids(case_id)) STRICT;
CREATE TABLE source_spans(case_id TEXT NOT NULL,span_index INTEGER NOT NULL CHECK(span_index>=0),pdf_page_index INTEGER NOT NULL CHECK(pdf_page_index>=1),text_sha256 TEXT NOT NULL,x0 REAL NOT NULL,y0 REAL NOT NULL,x1 REAL NOT NULL,y1 REAL NOT NULL,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.pdf_page_index')=pdf_page_index AND json_extract(payload_json,'$.text_sha256')=text_sha256),PRIMARY KEY(case_id,span_index),FOREIGN KEY(case_id) REFERENCES cases(case_id)) STRICT;
CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('question','answer','basis','facts','table')),sequence INTEGER NOT NULL CHECK(sequence>=1),payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.chunk_id')=chunk_id AND json_extract(payload_json,'$.case_id')=case_id AND json_extract(payload_json,'$.role')=role AND json_extract(payload_json,'$.sequence')=sequence),UNIQUE(case_id,role,sequence),UNIQUE(chunk_id,case_id),FOREIGN KEY(case_id) REFERENCES cases(case_id)) STRICT;
CREATE TABLE chunk_source_spans(chunk_id TEXT NOT NULL,case_id TEXT NOT NULL,span_index INTEGER NOT NULL,PRIMARY KEY(chunk_id,span_index),FOREIGN KEY(chunk_id,case_id) REFERENCES chunks(chunk_id,case_id),FOREIGN KEY(case_id,span_index) REFERENCES source_spans(case_id,span_index)) STRICT;
CREATE TABLE law_refs(law_ref_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,source_span_index INTEGER NOT NULL,citation_ordinal INTEGER NOT NULL CHECK(citation_ordinal>=1),binding_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.law_ref_id')=law_ref_id AND json_extract(payload_json,'$.case_id')=case_id),FOREIGN KEY(case_id,source_span_index) REFERENCES source_spans(case_id,span_index)) STRICT;
CREATE TABLE case_relations(relation_id TEXT PRIMARY KEY,source_case_id TEXT NOT NULL,target_case_id TEXT NOT NULL,relation_type TEXT NOT NULL,approval_sha256 TEXT NOT NULL,evidence_sha256 TEXT NOT NULL,reviewer_id TEXT NOT NULL,source_content_sha256 TEXT NOT NULL,target_content_sha256 TEXT NOT NULL,binding_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.relation_id')=relation_id),FOREIGN KEY(source_case_id) REFERENCES cases(case_id),FOREIGN KEY(target_case_id) REFERENCES cases(case_id),CHECK(source_case_id<>target_case_id)) STRICT;
CREATE TABLE corrections(correction_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>=1),target_field TEXT NOT NULL,before_sha256 TEXT NOT NULL,after_sha256 TEXT NOT NULL,promotion_envelope_sha256 TEXT NOT NULL,binding_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),FOREIGN KEY(case_id) REFERENCES cases(case_id),UNIQUE(case_id,target_field,sequence)) STRICT;
CREATE TABLE review_events(event_id TEXT PRIMARY KEY,case_id TEXT NOT NULL,event_sequence INTEGER NOT NULL CHECK(event_sequence>=1),payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),FOREIGN KEY(case_id) REFERENCES cases(case_id),UNIQUE(case_id,event_sequence)) STRICT;
CREATE TABLE ingestion_runs(run_id TEXT PRIMARY KEY,release_id TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_extract(payload_json,'$.run_id')=run_id AND json_extract(payload_json,'$.release_id')=release_id)) STRICT;
CREATE TABLE tokenizer_contract(singleton INTEGER PRIMARY KEY CHECK(singleton=1),model_name TEXT NOT NULL,revision TEXT NOT NULL,model_lock_sha256 TEXT NOT NULL,runtime_fingerprint_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL CHECK(json_valid(payload_json))) STRICT;
CREATE TABLE review_registry(singleton INTEGER PRIMARY KEY CHECK(singleton=1),fingerprint_sha256 TEXT NOT NULL,canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json))) STRICT;
CREATE TABLE review_registry_locations(case_id TEXT NOT NULL,sequence INTEGER NOT NULL CHECK(sequence>=1),source_span_index INTEGER NOT NULL CHECK(source_span_index>=0),reason_code TEXT NOT NULL,finding_count INTEGER NOT NULL CHECK(finding_count>=1),PRIMARY KEY(case_id,sequence),FOREIGN KEY(case_id,source_span_index) REFERENCES source_spans(case_id,span_index)) STRICT;
CREATE TABLE case_authorities(case_id TEXT PRIMARY KEY,promotion_envelope_sha256 TEXT NOT NULL,parser_authority_sha256 TEXT NOT NULL,raw_authority_sha256 TEXT NOT NULL,role_authority_sha256 TEXT,table_evidence_json TEXT NOT NULL CHECK(json_valid(table_evidence_json)),chunk_set_sha256 TEXT,review_snapshot_sha256 TEXT NOT NULL,CHECK((role_authority_sha256 IS NULL)=(chunk_set_sha256 IS NULL)),FOREIGN KEY(case_id) REFERENCES cases(case_id)) STRICT;
"""


def _create_immutable_triggers(connection: sqlite3.Connection) -> None:
    tables = (
        "build_meta",
        "documents",
        "issued_case_ids",
        "cases",
        "source_spans",
        "chunks",
        "chunk_source_spans",
        "law_refs",
        "case_relations",
        "corrections",
        "review_events",
        "ingestion_runs",
        "tokenizer_contract",
        "review_registry",
        "review_registry_locations",
        "case_authorities",
    )
    for table in tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER immutable_{table}_{operation.lower()} BEFORE {operation} ON \"{table}\" BEGIN SELECT RAISE(ABORT,'canonical table is immutable'); END"
            )


def _trusted_output_parent(path: Path) -> bool:
    absolute = Path(os.path.abspath(path.parent))
    current = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:]:
            current /= component
            details = os.lstat(current)
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                return False
    except OSError:
        return False
    return True


def _revalidate_review_registry(
    value: object, *, expected_sha256: str
) -> VerifiedCanonicalReviewRegistry:
    if type(value) is not VerifiedCanonicalReviewRegistry or not _valid_sha256(
        expected_sha256
    ):
        _raise("review registry authority is invalid")
    try:
        raw = value.to_bytes()
        verified = CanonicalReviewRegistry.from_bytes(
            raw,
            expected_sha256=expected_sha256,
        )
    except (AttributeError, TypeError, ValueError):
        verified = None
    if verified is None:
        _raise("review registry authority is invalid")
    return verified


def _validated_batch(
    batch: object,
    *,
    expected_review_sha: str,
    expected_registry_sha: str,
    expected_chunk_shas: object,
    expected_relation_approval_shas: object,
    expected_model_lock_sha: str,
    expected_runtime_sha: str,
    prior_issued_records: tuple[IssuedCaseRecord, ...],
) -> tuple[
    CanonicalStorageBatch,
    tuple[Document, ...],
    tuple[Case, ...],
    tuple[VerifiedChunkSet, ...],
    tuple[VerifiedLawRef, ...],
    tuple[VerifiedCaseRelation, ...],
    tuple[IngestionRun, ...],
    tuple[VerifiedPromotionEnvelope, ...],
    VerifiedReviewDecisionSnapshot,
]:
    if type(batch) is not CanonicalStorageBatch or not _valid_release(batch.release_id):
        _raise("canonical storage batch is invalid")
    collections = (
        batch.documents,
        batch.cases,
        batch.chunk_sets,
        batch.law_refs,
        batch.relations,
        batch.ingestion_runs,
        batch.promotion_envelopes,
    )
    if any(
        type(items) is not tuple or len(items) > _MAX_RECORDS for items in collections
    ):
        _raise("canonical storage batch is invalid")
    documents = tuple(_revalidate_simple(item, Document) for item in batch.documents)
    cases = tuple(_revalidate_case(item) for item in batch.cases)
    runs = tuple(_revalidate_run(item) for item in batch.ingestion_runs)
    if (
        any(item is None for item in (*documents, *cases, *runs))
        or not documents
        or not cases
    ):
        _raise("canonical storage batch is invalid")
    approved_documents = cast(tuple[Document, ...], documents)
    approved_cases = cast(tuple[Case, ...], cases)
    approved_runs = cast(tuple[IngestionRun, ...], runs)
    if (
        len({item.doc_id for item in approved_documents}) != len(approved_documents)
        or len({item.case_id for item in approved_cases}) != len(approved_cases)
        or any(
            not _valid_source_filename(item.source_filename)
            for item in approved_documents
        )
    ):
        _raise("canonical storage batch contains duplicate IDs")
    case_by_id = {case.case_id: case for case in approved_cases}
    document_by_id = {document.doc_id: document for document in approved_documents}
    if any(case.doc_id not in document_by_id for case in approved_cases):
        _raise("canonical case document is unavailable")
    if any(
        span.pdf_page_index > document_by_id[case.doc_id].pdf_page_count
        for case in approved_cases
        for span in case.source_spans
    ):
        _raise("source span page is outside the owning document")
    contract = _revalidate_contract(batch.tokenizer_contract)
    if (
        contract is None
        or contract.model_lock_sha256 != expected_model_lock_sha
        or contract.runtime_fingerprint_sha256 != expected_runtime_sha
    ):
        _raise("tokenizer contract does not match external authority")
    registry = _revalidate_review_registry(
        batch.review_registry,
        expected_sha256=expected_registry_sha,
    )
    references = {reference.case_id: reference for reference in registry.cases}
    if set(references) != set(case_by_id):
        _raise("review registry case set is invalid")
    envelopes: list[VerifiedPromotionEnvelope] = []
    for envelope in batch.promotion_envelopes:
        if type(envelope) is not VerifiedPromotionEnvelope:
            _raise("promotion envelope authority is invalid")
        try:
            case_id = envelope.candidate_case.case_id
        except (AttributeError, TypeError):
            _raise("promotion envelope authority is invalid")
        reference = references.get(case_id)
        if reference is None:
            _raise("promotion envelope authority is invalid")
        try:
            verified_envelope = load_promotion_envelope(
                envelope.canonical_bytes,
                expected_sha256=reference.content_sha256,
            )
        except (AttributeError, StorageError):
            _raise("promotion envelope authority is invalid")
        if any(
            sum(
                span.pdf_page_index == location.page_id and span.bbox == location.bbox
                for span in verified_envelope.candidate_case.source_spans
            )
            != 1
            for location in reference.source_locations
        ):
            _raise("review registry provenance does not match promotion envelope")
        envelopes.append(verified_envelope)
    envelope_by_case = {
        envelope.candidate_case.case_id: envelope for envelope in envelopes
    }
    if len(envelope_by_case) != len(envelopes) or set(envelope_by_case) != set(
        case_by_id
    ):
        _raise("promotion envelope case set is invalid")
    if type(batch.review_decision_snapshot) is not VerifiedReviewDecisionSnapshot:
        _raise("review decision snapshot is required")
    try:
        snapshot = load_review_decision_snapshot(
            batch.review_decision_snapshot.canonical_bytes,
            expected_sha256=expected_review_sha,
        )
    except (AttributeError, StorageError):
        _raise("review decision snapshot is invalid")
    if snapshot.registry_fingerprint_sha256 != registry.fingerprint_sha256:
        _raise("review registry authority does not match")
    decisions = {item.case_id: item for item in snapshot.cases}
    if set(decisions) != set(case_by_id):
        _raise("review decision snapshot case set is invalid")
    for case_id, case in case_by_id.items():
        decision = decisions[case_id]
        record = decision.review_record
        candidate_payload = envelope_by_case[case_id].candidate_case.model_dump(
            mode="json"
        )
        candidate_payload.update(
            {
                "answer_eligible": record["answer_eligible"],
                "critical_field_review": record["critical_field_review"],
                "review_status": record["review_status"],
                "search_eligible": record["search_eligible"],
            }
        )
        try:
            derived_case = Case.model_validate_json(_canonical_json(candidate_payload))
        except (TypeError, ValueError):
            derived_case = None
        if (
            envelope_by_case[case_id].fingerprint_sha256
            != decision.promotion_envelope_sha256
            or derived_case is None
            or canonical_case_sha256(derived_case) != canonical_case_sha256(case)
            or decision.corrections
        ):
            _raise("review decision does not match canonical case")
    searchable_case_ids = {
        case.case_id for case in approved_cases if case.search_eligible
    }
    role_authorities: dict[str, str] = {}
    table_authorities: dict[str, dict[int, str]] = {}
    for case_id in searchable_case_ids:
        envelope = envelope_by_case[case_id]
        try:
            role_authorities[case_id] = hashlib.sha256(
                role_source_manifest_bytes(case_by_id[case_id], envelope.role_sources)
            ).hexdigest()
        except ValueError:
            _raise("role authority does not match canonical cases")
        table_pins: dict[int, str] = {}
        for source in envelope.role_sources:
            if source.table_evidence_sha256 is None:
                continue
            previous = table_pins.get(source.source_span_index)
            if previous is not None and previous != source.table_evidence_sha256:
                _raise("table evidence authority is invalid")
            table_pins[source.source_span_index] = source.table_evidence_sha256
        table_authorities[case_id] = table_pins
    if (
        type(expected_chunk_shas) is not dict
        or set(expected_chunk_shas) != searchable_case_ids
        or any(not _valid_sha256(value) for value in expected_chunk_shas.values())
    ):
        _raise("chunk-set authority does not match canonical cases")
    if len(batch.chunk_sets) != len(searchable_case_ids):
        _raise("verified chunk set case set is invalid")
    chunk_sets: list[VerifiedChunkSet] = []
    for item in batch.chunk_sets:
        if type(item) is not VerifiedChunkSet or not item.chunks:
            _raise("verified chunk set is required")
        case_id = item.chunks[0].case_id
        chunk_case = case_by_id.get(case_id)
        if chunk_case is None:
            _raise("verified chunk set case is invalid")
        try:
            chunk_sets.append(
                revalidate_verified_chunk_set(
                    item,
                    chunk_case,
                    contract=contract,
                    expected_role_authority_sha256=role_authorities[case_id],
                    expected_chunk_set_sha256=cast(dict[str, str], expected_chunk_shas)[
                        case_id
                    ],
                    expected_table_evidence_sha256s=table_authorities[case_id],
                )
            )
        except ValueError:
            _raise("verified chunk set is invalid")
    if {item.chunks[0].case_id for item in chunk_sets} != searchable_case_ids:
        _raise("verified chunk set case set is invalid")
    law_refs: list[VerifiedLawRef] = []
    for law_item in batch.law_refs:
        if type(law_item) is not VerifiedLawRef or law_item.case_id not in case_by_id:
            _raise("verified law reference is required")
        try:
            law_refs.append(
                revalidate_verified_law_ref(
                    law_item,
                    case_by_id[law_item.case_id],
                )
            )
        except ValueError:
            _raise("verified law reference is invalid")
    for case in approved_cases:
        ids = tuple(
            sorted(item.law_ref_id for item in law_refs if item.case_id == case.case_id)
        )
        if tuple(sorted(case.law_ref_ids)) != ids:
            _raise("law reference IDs do not match canonical case")
    if (
        type(batch.relation_approval_sha256s) is not dict
        or type(expected_relation_approval_shas) is not dict
        or batch.relation_approval_sha256s != expected_relation_approval_shas
    ):
        _raise("relation approval authority is invalid")
    relations: list[VerifiedCaseRelation] = []
    for relation_item in batch.relations:
        if type(relation_item) is not VerifiedCaseRelation:
            _raise("verified relation is required")
        relation = relation_item.relation
        expected = cast(dict[str, object], expected_relation_approval_shas).get(
            relation.relation_id
        )
        if (
            not _valid_sha256(expected)
            or relation.source_case_id not in case_by_id
            or relation.target_case_id not in case_by_id
        ):
            _raise("relation approval authority is invalid")
        try:
            relations.append(
                revalidate_verified_relation(
                    relation_item,
                    case_by_id[relation.source_case_id],
                    case_by_id[relation.target_case_id],
                    expected_approval_sha256=cast(str, expected),
                )
            )
        except ValueError:
            _raise("verified relation is invalid")
    if set(cast(dict[str, object], expected_relation_approval_shas)) != {
        item.relation.relation_id for item in relations
    }:
        _raise("relation approval authority is invalid")

    if len(approved_runs) != 1:
        _raise("ingestion run is not eligible for canonical storage")
    run = approved_runs[0]
    if (
        run.release_id != batch.release_id
        or run.ended_at is None
        or run.approved_by != f"review-snapshot:{snapshot.fingerprint_sha256}"
        or tuple(sorted(run.source_sha256s)) != run.source_sha256s
        or set(run.document_page_counts) != set(document_by_id)
        or len(run.source_sha256s) != len(set(run.source_sha256s))
        or set(run.source_sha256s)
        != {document.sha256 for document in approved_documents}
        or any(
            counts.succeeded + counts.quarantined + counts.failed
            != document_by_id[doc_id].pdf_page_count
            or counts.quarantined != 0
            or counts.failed != 0
            for doc_id, counts in run.document_page_counts.items()
        )
    ):
        _raise("ingestion run is not eligible for canonical storage")
    latest_review_at = max(
        cast(str, event["occurred_at"])
        for decision in snapshot.cases
        for event in decision.events
    )
    reviewed_at = datetime.fromisoformat(latest_review_at)
    if run.ended_at < reviewed_at:
        _raise("ingestion run ended before canonical review")

    prior_by_id = {record.case_id: record for record in prior_issued_records}
    prior_active = {
        case_id for case_id, record in prior_by_id.items() if record.state == "active"
    }
    current_case_ids = set(case_by_id)
    expected_created = current_case_ids - set(prior_by_id)
    expected_deleted = prior_active - current_case_ids
    expected_changed = {
        case_id
        for case_id in current_case_ids & prior_active
        if canonical_case_sha256(case_by_id[case_id])
        != prior_by_id[case_id].current_content_sha256
    }
    run_deltas = (
        run.created_case_ids,
        run.changed_case_ids,
        run.deleted_case_ids,
    )
    if (
        any(
            tuple(sorted(values)) != values or len(values) != len(set(values))
            for values in run_deltas
        )
        or set(run.created_case_ids) != expected_created
        or set(run.changed_case_ids) != expected_changed
        or set(run.deleted_case_ids) != expected_deleted
    ):
        _raise("ingestion run case delta is invalid")
    return (
        batch,
        approved_documents,
        approved_cases,
        tuple(chunk_sets),
        tuple(law_refs),
        tuple(relations),
        approved_runs,
        tuple(envelopes),
        snapshot,
    )


def write_canonical_storage(
    path: Path,
    batch: object,
    *,
    issuance_lease: IssuanceLease,
    expected_review_decision_snapshot_sha256: str,
    expected_registry_sha256: str,
    expected_chunk_set_sha256s: dict[str, str],
    expected_relation_approval_sha256s: dict[str, str],
    expected_model_lock_sha256: str,
    expected_runtime_fingerprint_sha256: str,
) -> StorageProjectionReceipt:
    """Write one immutable DB atomically while a persistent issuance lease is held."""
    if path.exists() or path.is_symlink():
        _raise("canonical storage target must not exist")
    if not _trusted_output_parent(path):
        _raise("canonical storage output parent is untrusted")
    if type(issuance_lease) is not IssuanceLease or issuance_lease._closed:
        _raise("active issuance lease is required")
    prior_issued_records = _issuance_records(issuance_lease._connection)
    validated = _validated_batch(
        batch,
        expected_review_sha=expected_review_decision_snapshot_sha256,
        expected_registry_sha=expected_registry_sha256,
        expected_chunk_shas=expected_chunk_set_sha256s,
        expected_relation_approval_shas=expected_relation_approval_sha256s,
        expected_model_lock_sha=expected_model_lock_sha256,
        expected_runtime_sha=expected_runtime_fingerprint_sha256,
        prior_issued_records=prior_issued_records,
    )
    (
        _,
        documents,
        cases,
        chunk_sets,
        law_refs,
        relations,
        runs,
        envelopes,
        snapshot,
    ) = validated
    issued = issuance_lease.projected_records(
        cases, cast(CanonicalStorageBatch, batch).release_id
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.executescript(_CANONICAL_SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        canonical_batch = cast(CanonicalStorageBatch, batch)
        connection.execute(
            "INSERT INTO build_meta VALUES(1,?,?,?,?)",
            (
                canonical_batch.release_id,
                issuance_lease.head.bundle_sha256,
                snapshot.fingerprint_sha256,
                snapshot.registry_fingerprint_sha256,
            ),
        )
        for document in documents:
            connection.execute(
                "INSERT INTO documents VALUES(?,?,?,?)",
                (
                    document.doc_id,
                    document.sha256,
                    document.pdf_page_count,
                    _model_json(document),
                ),
            )
        for record in issued:
            connection.execute(
                "INSERT INTO issued_case_ids VALUES(?,?,?,?,?,?)",
                (
                    record.case_id,
                    record.state,
                    record.first_release_id,
                    record.first_content_sha256,
                    record.current_content_sha256,
                    record.retired_release_id,
                ),
            )
        for case in cases:
            connection.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?)",
                (
                    case.case_id,
                    case.doc_id,
                    canonical_case_sha256(case),
                    case.review_status,
                    int(case.search_eligible),
                    int(case.answer_eligible),
                    _model_json(case),
                ),
            )
            for index, span in enumerate(case.source_spans):
                connection.execute(
                    "INSERT INTO source_spans VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        case.case_id,
                        index,
                        span.pdf_page_index,
                        span.text_sha256,
                        *span.bbox,
                        _model_json(span),
                    ),
                )
        stored_case_by_id = {case.case_id: case for case in cases}
        registry = canonical_batch.review_registry
        connection.execute(
            "INSERT INTO review_registry VALUES(1,?,?)",
            (
                registry.fingerprint_sha256,
                registry.to_bytes().decode("utf-8"),
            ),
        )
        for reference in registry.cases:
            for sequence, location in enumerate(reference.source_locations, start=1):
                source_span_index = next(
                    index
                    for index, span in enumerate(
                        stored_case_by_id[reference.case_id].source_spans
                    )
                    if span.pdf_page_index == location.page_id
                    and span.bbox == location.bbox
                )
                connection.execute(
                    "INSERT INTO review_registry_locations VALUES(?,?,?,?,?)",
                    (
                        reference.case_id,
                        sequence,
                        source_span_index,
                        location.reason_code,
                        location.count,
                    ),
                )
        for chunk_set in chunk_sets:
            for chunk in chunk_set.chunks:
                connection.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?)",
                    (
                        chunk.chunk_id,
                        chunk.case_id,
                        chunk.role,
                        chunk.sequence,
                        _model_json(chunk),
                    ),
                )
                for index in chunk.source_span_indexes:
                    connection.execute(
                        "INSERT INTO chunk_source_spans VALUES(?,?,?)",
                        (chunk.chunk_id, chunk.case_id, index),
                    )
        for law_ref in law_refs:
            connection.execute(
                "INSERT INTO law_refs VALUES(?,?,?,?,?,?)",
                (
                    law_ref.law_ref_id,
                    law_ref.case_id,
                    law_ref.source_span_index,
                    law_ref.citation_ordinal,
                    law_ref.binding_sha256,
                    _model_json(law_ref.law_ref),
                ),
            )
        for verified_relation in relations:
            relation = verified_relation.relation
            approval = verified_relation.approval
            connection.execute(
                "INSERT INTO case_relations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    relation.relation_id,
                    relation.source_case_id,
                    relation.target_case_id,
                    relation.relation_type,
                    verified_relation.approval_sha256,
                    approval.evidence_sha256,
                    approval.reviewer_id,
                    verified_relation.source_content_sha256,
                    verified_relation.target_content_sha256,
                    verified_relation.binding_sha256,
                    _model_json(relation),
                ),
            )
        for envelope in envelopes:
            for correction in envelope.corrections:
                binding = hashlib.sha256(
                    b"sen-qa-stored-correction-v1\0"
                    + bytes.fromhex(envelope.fingerprint_sha256)
                    + _canonical_json(correction)
                ).hexdigest()
                payload = {
                    **correction,
                    "binding_sha256": binding,
                    "promotion_envelope_sha256": envelope.fingerprint_sha256,
                }
                connection.execute(
                    "INSERT INTO corrections VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        correction["correction_id"],
                        envelope.candidate_case.case_id,
                        correction["sequence"],
                        correction["target_field"],
                        correction["before_sha256"],
                        correction["after_sha256"],
                        envelope.fingerprint_sha256,
                        binding,
                        _canonical_json(payload).decode(),
                    ),
                )
        for decision in snapshot.cases:
            for event in decision.events:
                payload = {"case_id": decision.case_id, **event}
                connection.execute(
                    "INSERT INTO review_events VALUES(?,?,?,?)",
                    (
                        event["event_id"],
                        decision.case_id,
                        event["event_sequence"],
                        _canonical_json(payload).decode(),
                    ),
                )
        for run in runs:
            connection.execute(
                "INSERT INTO ingestion_runs VALUES(?,?,?)",
                (run.run_id, run.release_id, _model_json(run)),
            )
        contract = canonical_batch.tokenizer_contract
        contract_payload = {
            "model_lock_sha256": contract.model_lock_sha256,
            "model_name": contract.model_name,
            "revision": contract.revision,
            "runtime_fingerprint_sha256": contract.runtime_fingerprint_sha256,
        }
        connection.execute(
            "INSERT INTO tokenizer_contract VALUES(1,?,?,?,?,?)",
            (
                contract.model_name,
                contract.revision,
                contract.model_lock_sha256,
                contract.runtime_fingerprint_sha256,
                _canonical_json(contract_payload).decode(),
            ),
        )
        chunks_by_case = {item.chunks[0].case_id: item for item in chunk_sets}
        envelopes_by_case = {item.candidate_case.case_id: item for item in envelopes}
        for case in cases:
            stored_chunk_set = chunks_by_case.get(case.case_id)
            envelope = envelopes_by_case[case.case_id]
            role_authority = (
                hashlib.sha256(
                    role_source_manifest_bytes(case, envelope.role_sources)
                ).hexdigest()
                if case.search_eligible
                else None
            )
            table_evidence = {
                str(source.source_span_index): source.table_evidence_sha256
                for source in envelope.role_sources
                if source.table_evidence_sha256 is not None
            }
            connection.execute(
                "INSERT INTO case_authorities VALUES(?,?,?,?,?,?,?,?)",
                (
                    case.case_id,
                    envelope.fingerprint_sha256,
                    envelope.parser_authority_sha256,
                    envelope.raw_authority_sha256,
                    role_authority,
                    _canonical_json(table_evidence).decode(),
                    stored_chunk_set.binding_sha256
                    if stored_chunk_set is not None
                    else None,
                    snapshot.fingerprint_sha256,
                ),
            )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _raise("canonical storage foreign key verification failed")
        _create_immutable_triggers(connection)
        connection.execute("COMMIT")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        connection = None
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        bundle_sha256 = _regular_file_sha256(
            temporary,
            max_bytes=_MAX_DATABASE_BYTES,
        )
        with _open_parent_directory(path) as (directory_fd, leaf, _current_parent):
            if (
                _leaf_details(directory_fd, leaf) is not None
                or _leaf_details(directory_fd, temporary.name) is None
            ):
                _raise("canonical storage target must not exist")
            _publish_file_noreplace(
                directory_fd=directory_fd,
                temporary_leaf=temporary.name,
                final_leaf=leaf,
            )
            os.fsync(directory_fd)
        return issuance_lease._issue_projection_receipt(
            release_id=cast(CanonicalStorageBatch, batch).release_id,
            bundle_sha256=bundle_sha256,
            records=issued,
        )
    except (sqlite3.Error, OSError, StorageError):
        if connection is not None:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
        for candidate in (
            temporary,
            Path(str(temporary) + "-wal"),
            Path(str(temporary) + "-shm"),
        ):
            try:
                candidate.unlink()
            except OSError:
                pass
        _raise("canonical storage write failed")


@contextmanager
def connect_canonical_storage(path: Path) -> Iterator[sqlite3.Connection]:
    stable_path = _require_regular_database(path, max_bytes=_MAX_DATABASE_BYTES)
    connection: sqlite3.Connection | None = None
    failed = False
    try:
        connection = sqlite3.connect(
            _sqlite_uri(stable_path, mode="ro"), uri=True, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA recursive_triggers=ON")
        connection.execute("PRAGMA query_only=ON")
        if (
            str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            != "wal"
            or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or connection.execute("PRAGMA foreign_key_check").fetchall()
        ):
            failed = True
    except sqlite3.Error:
        failed = True
    if failed or connection is None:
        if connection is not None:
            connection.close()
        _raise("canonical storage database is invalid")
    try:
        yield connection
    finally:
        connection.close()


def export_canonical_jsonl(
    path: Path, target: Path, *, fault_after_records: int | None = None
) -> dict[str, str]:
    """Export every canonical table in stable PK order through one atomic directory rename."""
    if target.exists() or target.is_symlink():
        _raise("canonical export target must not exist")
    if not _trusted_output_parent(target):
        _raise("canonical export output parent is untrusted")
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    written = 0
    total_bytes = 0
    hashes: dict[str, str] = {}
    try:
        with connect_canonical_storage(path) as connection:
            connection.execute("BEGIN")
            for table in _EXPORT_TABLES:
                columns = [
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                ]
                order_columns = [
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info("{table}")')
                    if int(row[5]) > 0
                ]
                order = ",".join(f'"{name}"' for name in order_columns) or "rowid"
                rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY {order}')
                destination = temporary / f"{table}.jsonl"
                digest = hashlib.sha256()
                with destination.open("xb") as handle:
                    for row in rows:
                        if written >= _MAX_RECORDS:
                            _raise("canonical export failed")
                        record = dict(zip(columns, row, strict=True))
                        if "payload_json" in record:
                            payload = json.loads(
                                cast(str, record.pop("payload_json")),
                                object_pairs_hook=_unique_object,
                            )
                            if type(payload) is not dict:
                                _raise("canonical export failed")
                            record = {**record, "payload": payload}
                        line = _canonical_json(record) + b"\n"
                        total_bytes += len(line)
                        if total_bytes > _MAX_EXPORT_BYTES:
                            _raise("canonical export failed")
                        handle.write(line)
                        digest.update(line)
                        written += 1
                        if (
                            fault_after_records is not None
                            and written >= fault_after_records
                        ):
                            _raise("canonical export failed")
                    handle.flush()
                    os.fsync(handle.fileno())
                hashes[destination.name] = digest.hexdigest()
            connection.execute("COMMIT")
        directory_fd = os.open(temporary, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(directory_fd)
        os.close(directory_fd)
        os.rename(temporary, target)
        parent_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(parent_fd)
        os.close(parent_fd)
        return hashes
    except (OSError, sqlite3.Error, StorageError, ValueError):
        shutil.rmtree(temporary, ignore_errors=True)
        _raise("canonical export failed")
