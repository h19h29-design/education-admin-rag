"""Fail-closed manual-review state and append-only audit storage.

The review database deliberately stores identifiers, hashes, state, and reviewer
attestations only. Canonical candidate text remains in the read-only corpus.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self, cast, get_args

from src.corpus.ids import validate_case_id
from src.ingestion.quality import QualityReason, ReviewProvenanceReason

ReviewStatus = Literal[
    "machine_extracted", "needs_review", "search_approved", "approved", "rejected"
]
ReviewPurpose = Literal["search", "answer"]
RunMode = Literal["critical-fields-all", "answer-and-basis-all"]

_SCHEMA_VERSION = 1
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.:@/-]{1,160}$")
_MANIFEST_SCHEMA = "sen-qa-review-segment/v1"
_REGISTRY_SCHEMA = "sen-qa-canonical-review-registry/v1"
_DECISION_SNAPSHOT_SCHEMA = "review-decision-snapshot-v1"
_MAX_DECISION_SNAPSHOT_CASES = 10_000
_MAX_DECISION_SNAPSHOT_EVENTS = 50_000
_MAX_DECISION_SNAPSHOT_BYTES = 16 * 1024 * 1024
_SNAPSHOT_BOUNDED_ERROR = "review decision snapshot exceeds bounded export limits"
_SNAPSHOT_COMPLETE_ERROR = (
    "review registry requires a complete terminal decision snapshot"
)
_SNAPSHOT_INVALID_ERROR = "review decision snapshot is invalid"
_RESTRICTED_APPROVAL_ERROR = "restricted candidate cannot be approved"
_TERMINAL_STATES = {"approved", "rejected"}
_OS_ACTOR_PATTERN = re.compile(r"^uid:([0-9]+):")
_INVALID_DUPLICATE_PAGE = re.compile(r"(?:^|-)p0(?:-|$)")
_QUALITY_REASON_CODES = frozenset(cast(tuple[str, ...], get_args(QualityReason)))
_PROVENANCE_REASON_CODES = _QUALITY_REASON_CODES | {"human-review-required"}
_REVIEW_REASON_CODES = _PROVENANCE_REASON_CODES | {
    "answer_batch",
    "answer_checked",
    "bad_layout",
    "critical_batch",
    "fields_checked",
    "invalid_layout",
    "late_reject",
    "manual_run",
    "quality_gate",
    "repeat",
    "retry",
    "search_checked",
    "segment_checked",
    "segment_sample_checked",
    "skip_fields",
    "skip_search",
}

_REVIEW_SCHEMA_SQL = """
CREATE TABLE review_schema_migrations (
    schema_version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE review_registry_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL UNIQUE
        CHECK (length(fingerprint_sha256) = 64),
    loaded_at TEXT NOT NULL
);

CREATE TABLE canonical_review_bindings (
    case_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    registry_sha256 TEXT NOT NULL,
    UNIQUE (case_id, content_sha256),
    FOREIGN KEY (registry_sha256)
        REFERENCES review_registry_meta(fingerprint_sha256)
);

CREATE TABLE canonical_review_locations (
    case_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    page_id INTEGER NOT NULL CHECK (page_id >= 1),
    x0 REAL NOT NULL,
    y0 REAL NOT NULL,
    x1 REAL NOT NULL,
    y1 REAL NOT NULL,
    reason_code TEXT NOT NULL,
    finding_count INTEGER NOT NULL CHECK (finding_count >= 1),
    PRIMARY KEY (case_id, sequence),
    FOREIGN KEY (case_id) REFERENCES canonical_review_bindings(case_id)
);

CREATE TABLE review_cases (
    case_id TEXT PRIMARY KEY,
    content_sha256 TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (
        review_status IN (
            'machine_extracted', 'needs_review', 'search_approved',
            'approved', 'rejected'
        )
    ),
    critical_field_review TEXT NOT NULL CHECK (
        critical_field_review IN ('pending', 'verified')
    ),
    search_eligible INTEGER NOT NULL CHECK (search_eligible IN (0, 1)),
    answer_eligible INTEGER NOT NULL CHECK (answer_eligible IN (0, 1)),
    critical_reviewer_id TEXT,
    search_reviewer_id TEXT,
    answer_reviewer_id TEXT,
    content_verified INTEGER NOT NULL CHECK (content_verified IN (0, 1)),
    basis_verified INTEGER NOT NULL CHECK (basis_verified IN (0, 1)),
    privacy_verified INTEGER NOT NULL CHECK (privacy_verified IN (0, 1)),
    queued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 0),
    UNIQUE (case_id, content_sha256),
    FOREIGN KEY (case_id, content_sha256)
        REFERENCES canonical_review_bindings(case_id, content_sha256),
    CHECK (
        (review_status = 'machine_extracted'
            AND critical_field_review = 'pending'
            AND search_eligible = 0 AND answer_eligible = 0)
        OR (review_status = 'needs_review'
            AND search_eligible = 0 AND answer_eligible = 0)
        OR (review_status = 'search_approved'
            AND critical_field_review = 'verified'
            AND search_eligible = 1 AND answer_eligible = 0
            AND critical_reviewer_id IS NOT NULL
            AND search_reviewer_id IS NOT NULL)
        OR (review_status = 'approved'
            AND critical_field_review = 'verified'
            AND search_eligible = 1 AND answer_eligible = 1
            AND critical_reviewer_id IS NOT NULL
            AND search_reviewer_id IS NOT NULL
            AND answer_reviewer_id IS NOT NULL
            AND content_verified = 1 AND basis_verified = 1
            AND privacy_verified = 1)
        OR (review_status = 'rejected'
            AND search_eligible = 0 AND answer_eligible = 0)
    )
);

CREATE TABLE review_batch_manifests (
    manifest_sha256 TEXT PRIMARY KEY CHECK (length(manifest_sha256) = 64),
    segment_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    case_count INTEGER NOT NULL CHECK (case_count >= 1)
);

CREATE TABLE review_events (
    event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ('enqueue', 'verify_fields', 'approve_search', 'approve_answer', 'reject')
    ),
    before_state TEXT NOT NULL CHECK (
        before_state IN (
            'machine_extracted', 'needs_review', 'search_approved',
            'approved', 'rejected'
        )
    ),
    after_state TEXT NOT NULL CHECK (
        after_state IN (
            'machine_extracted', 'needs_review', 'search_approved',
            'approved', 'rejected'
        )
    ),
    actor_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    reviewed_content_sha256 TEXT NOT NULL,
    batch_manifest_sha256 TEXT,
    FOREIGN KEY (case_id, reviewed_content_sha256)
        REFERENCES review_cases(case_id, content_sha256),
    CHECK (
        (action = 'enqueue'
            AND before_state = 'machine_extracted' AND after_state = 'needs_review')
        OR (action = 'verify_fields'
            AND before_state = 'needs_review' AND after_state = 'needs_review')
        OR (action = 'approve_search'
            AND before_state = 'needs_review' AND after_state = 'search_approved')
        OR (action = 'approve_answer'
            AND before_state = 'search_approved' AND after_state = 'approved')
        OR (action = 'reject'
            AND before_state IN ('machine_extracted', 'needs_review', 'search_approved')
            AND after_state = 'rejected')
    )
);

CREATE TRIGGER review_cases_insert_guard
BEFORE INSERT ON review_cases
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER review_cases_update_guard
BEFORE UPDATE ON review_cases
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER review_cases_delete_guard
BEFORE DELETE ON review_cases
BEGIN
    SELECT RAISE(ABORT, 'review state is not deletable');
END;

CREATE TRIGGER review_events_insert_guard
BEFORE INSERT ON review_events
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER review_events_update_guard
BEFORE UPDATE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review events are append-only');
END;

CREATE TRIGGER review_events_delete_guard
BEFORE DELETE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'review events are append-only');
END;

CREATE TRIGGER review_batch_insert_guard
BEFORE INSERT ON review_batch_manifests
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER review_batch_update_guard
BEFORE UPDATE ON review_batch_manifests
BEGIN
    SELECT RAISE(ABORT, 'review batch manifests are append-only');
END;

CREATE TRIGGER review_batch_delete_guard
BEFORE DELETE ON review_batch_manifests
BEGIN
    SELECT RAISE(ABORT, 'review batch manifests are append-only');
END;

CREATE TRIGGER canonical_bindings_update_guard
BEFORE UPDATE ON canonical_review_bindings
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER canonical_bindings_insert_guard
BEFORE INSERT ON canonical_review_bindings
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER canonical_bindings_delete_guard
BEFORE DELETE ON canonical_review_bindings
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER canonical_locations_update_guard
BEFORE UPDATE ON canonical_review_locations
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER canonical_locations_insert_guard
BEFORE INSERT ON canonical_review_locations
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER canonical_locations_delete_guard
BEFORE DELETE ON canonical_review_locations
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER registry_meta_update_guard
BEFORE UPDATE ON review_registry_meta
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER registry_meta_insert_guard
BEFORE INSERT ON review_registry_meta
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER registry_meta_delete_guard
BEFORE DELETE ON review_registry_meta
BEGIN
    SELECT RAISE(ABORT, 'canonical review registry is immutable');
END;

CREATE TRIGGER schema_migrations_insert_guard
BEFORE INSERT ON review_schema_migrations
WHEN review_write_authorized() != 1
BEGIN
    SELECT RAISE(ABORT, 'review_write_authorized required');
END;

CREATE TRIGGER schema_migrations_update_guard
BEFORE UPDATE ON review_schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'review schema migrations are append-only');
END;

CREATE TRIGGER schema_migrations_delete_guard
BEFORE DELETE ON review_schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'review schema migrations are append-only');
END;
"""


class ReviewError(ValueError):
    """Base review failure with a safe machine-readable CLI code."""

    code = "review_error"


class ReviewValidationError(ReviewError):
    """Input does not satisfy the review contract."""

    code = "invalid_input"


class ReviewConflictError(ReviewError):
    """Stored state conflicts with an expected state, hash, or event."""

    code = "review_conflict"


class ReviewNotFoundError(ReviewError):
    """A requested case is not present in the review queue."""

    code = "not_found"


@dataclass(frozen=True, slots=True)
class ReviewSourceLocation:
    """Value-free source provenance signed into the canonical registry."""

    page_id: int
    bbox: tuple[float, float, float, float]
    reason_code: ReviewProvenanceReason
    count: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_id, bool)
            or not isinstance(self.page_id, int)
            or self.page_id < 1
        ):
            raise ReviewValidationError("source page_id must be a positive integer")
        if (
            not isinstance(self.bbox, tuple)
            or len(self.bbox) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in self.bbox
            )
        ):
            raise ReviewValidationError("source bbox must contain four numbers")
        normalized_bbox = cast(
            tuple[float, float, float, float],
            tuple(float(value) for value in self.bbox),
        )
        object.__setattr__(self, "bbox", normalized_bbox)
        x0, y0, x1, y1 = self.bbox
        if not all(math.isfinite(value) for value in self.bbox) or x0 >= x1 or y0 >= y1:
            raise ReviewValidationError("source bbox must be finite and ordered")
        _validate_provenance_reason(self.reason_code)
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or self.count < 1
        ):
            raise ReviewValidationError("source finding count must be positive")


@dataclass(frozen=True, slots=True)
class ReviewReference:
    """A content-addressed reference without candidate text."""

    case_id: str
    content_sha256: str
    source_locations: tuple[ReviewSourceLocation, ...] = ()

    def __post_init__(self) -> None:
        _validate_case_id(self.case_id)
        _validate_hash(self.content_sha256, "content_sha256")
        if len(set(self.source_locations)) != len(self.source_locations):
            raise ReviewValidationError("canonical source locations must be unique")
        if (
            tuple(sorted(self.source_locations, key=_source_location_key))
            != self.source_locations
        ):
            raise ReviewValidationError("canonical source locations must be sorted")


@dataclass(frozen=True, slots=True)
class CanonicalReviewRegistry:
    """Content-addressed, read-only case/provenance authority for review state."""

    cases: tuple[ReviewReference, ...]
    schema_version: str = _REGISTRY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != _REGISTRY_SCHEMA:
            raise ReviewValidationError("unsupported canonical registry schema")
        if not self.cases:
            raise ReviewValidationError("canonical registry must contain cases")
        case_ids = [case.case_id for case in self.cases]
        if case_ids != sorted(case_ids) or len(case_ids) != len(set(case_ids)):
            raise ReviewValidationError(
                "canonical registry cases must be unique and sorted"
            )
        if any(not case.source_locations for case in self.cases):
            raise ReviewValidationError("canonical registry requires source provenance")

    @classmethod
    def create(cls, *, cases: Iterable[ReviewReference]) -> CanonicalReviewRegistry:
        canonical_cases = (
            ReviewReference(
                case_id=case.case_id,
                content_sha256=case.content_sha256,
                source_locations=tuple(
                    sorted(case.source_locations, key=_source_location_key)
                ),
            )
            for case in cases
        )
        return cls(cases=tuple(sorted(canonical_cases, key=lambda item: item.case_id)))

    def to_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "cases": [
                {
                    "case_id": case.case_id,
                    "content_sha256": case.content_sha256,
                    "source_locations": [
                        {
                            "bbox": list(location.bbox),
                            "count": location.count,
                            "page_id": location.page_id,
                            "reason_code": location.reason_code,
                        }
                        for location in case.source_locations
                    ],
                }
                for case in self.cases
            ],
        }
        return (
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            + "\n"
        ).encode("utf-8")

    @property
    def fingerprint_sha256(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, expected_sha256: str
    ) -> VerifiedCanonicalReviewRegistry:
        _validate_hash(expected_sha256, "canonical_registry_sha256")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ReviewValidationError("canonical registry hash mismatch")
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ReviewValidationError,
        ) as error:
            raise ReviewValidationError(
                "canonical registry is not valid JSON"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "cases"}:
            raise ReviewValidationError("canonical registry fields do not match schema")
        raw_cases = payload["cases"]
        if not isinstance(raw_cases, list) or not isinstance(
            payload["schema_version"], str
        ):
            raise ReviewValidationError("canonical registry types do not match schema")
        cases: list[ReviewReference] = []
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict) or set(raw_case) != {
                "case_id",
                "content_sha256",
                "source_locations",
            }:
                raise ReviewValidationError(
                    "canonical registry case fields do not match schema"
                )
            raw_locations = raw_case["source_locations"]
            if (
                not isinstance(raw_case["case_id"], str)
                or not isinstance(raw_case["content_sha256"], str)
                or not isinstance(raw_locations, list)
            ):
                raise ReviewValidationError(
                    "canonical registry case types do not match schema"
                )
            locations: list[ReviewSourceLocation] = []
            for raw_location in raw_locations:
                if not isinstance(raw_location, dict) or set(raw_location) != {
                    "bbox",
                    "count",
                    "page_id",
                    "reason_code",
                }:
                    raise ReviewValidationError(
                        "canonical registry provenance fields do not match schema"
                    )
                bbox = raw_location["bbox"]
                if (
                    not isinstance(bbox, list)
                    or len(bbox) != 4
                    or any(
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        for value in bbox
                    )
                    or isinstance(raw_location["page_id"], bool)
                    or not isinstance(raw_location["page_id"], int)
                    or isinstance(raw_location["count"], bool)
                    or not isinstance(raw_location["count"], int)
                    or not isinstance(raw_location["reason_code"], str)
                ):
                    raise ReviewValidationError(
                        "canonical registry provenance types are invalid"
                    )
                locations.append(
                    ReviewSourceLocation(
                        page_id=raw_location["page_id"],
                        bbox=cast(
                            tuple[float, float, float, float],
                            tuple(float(value) for value in bbox),
                        ),
                        reason_code=cast(
                            ReviewProvenanceReason,
                            raw_location["reason_code"],
                        ),
                        count=raw_location["count"],
                    )
                )
            cases.append(
                ReviewReference(
                    case_id=raw_case["case_id"],
                    content_sha256=raw_case["content_sha256"],
                    source_locations=tuple(locations),
                )
            )
        registry = cls(cases=tuple(cases), schema_version=payload["schema_version"])
        if registry.to_bytes() != raw:
            raise ReviewValidationError("canonical registry is not canonical")
        verified = object.__new__(VerifiedCanonicalReviewRegistry)
        object.__setattr__(verified, "registry", registry)
        object.__setattr__(verified, "fingerprint_sha256", expected_sha256)
        return verified

    def reference(self, case_id: str) -> ReviewReference:
        _validate_case_id(case_id)
        for reference in self.cases:
            if reference.case_id == case_id:
                return reference
        raise ReviewNotFoundError("case is not in canonical registry")


@dataclass(frozen=True, slots=True, init=False)
class VerifiedCanonicalReviewRegistry:
    """Canonical registry bytes verified against a separately supplied digest."""

    registry: CanonicalReviewRegistry
    fingerprint_sha256: str

    @property
    def cases(self) -> tuple[ReviewReference, ...]:
        return self.registry.cases

    @property
    def schema_version(self) -> str:
        return self.registry.schema_version

    def to_bytes(self) -> bytes:
        return self.registry.to_bytes()

    def reference(self, case_id: str) -> ReviewReference:
        return self.registry.reference(case_id)


@dataclass(frozen=True, slots=True)
class SegmentManifest:
    """Canonical, byte-stable search-approval segment manifest."""

    segment_id: str
    cases: tuple[ReviewReference, ...]
    schema_version: str = _MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        _validate_identifier(self.segment_id, "segment_id")
        if self.schema_version != _MANIFEST_SCHEMA:
            raise ReviewValidationError("unsupported manifest schema")
        if not self.cases:
            raise ReviewValidationError("manifest must contain cases")
        case_ids = [case.case_id for case in self.cases]
        if case_ids != sorted(case_ids):
            raise ReviewValidationError("manifest cases must be sorted")
        if len(case_ids) != len(set(case_ids)):
            raise ReviewValidationError("manifest contains duplicate cases")

    @classmethod
    def create(
        cls, *, segment_id: str, cases: Iterable[ReviewReference]
    ) -> SegmentManifest:
        return cls(
            segment_id=segment_id,
            cases=tuple(sorted(cases, key=lambda item: item.case_id)),
        )

    def to_bytes(self) -> bytes:
        payload = {
            "schema_version": self.schema_version,
            "segment_id": self.segment_id,
            "cases": [
                {"case_id": case.case_id, "content_sha256": case.content_sha256}
                for case in self.cases
            ],
        }
        return (
            json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> SegmentManifest:
        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_unique_json_object
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ReviewValidationError,
        ) as error:
            raise ReviewValidationError(
                "manifest is not valid canonical JSON"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "segment_id",
            "cases",
        }:
            raise ReviewValidationError("manifest fields do not match schema")
        raw_cases = payload["cases"]
        if not isinstance(raw_cases, list):
            raise ReviewValidationError("manifest cases must be a list")
        cases: list[ReviewReference] = []
        for raw_case in raw_cases:
            if not isinstance(raw_case, dict) or set(raw_case) != {
                "case_id",
                "content_sha256",
            }:
                raise ReviewValidationError("manifest case fields do not match schema")
            if not isinstance(raw_case["case_id"], str) or not isinstance(
                raw_case["content_sha256"], str
            ):
                raise ReviewValidationError("manifest case fields must be strings")
            cases.append(
                ReviewReference(
                    case_id=raw_case["case_id"],
                    content_sha256=raw_case["content_sha256"],
                )
            )
        if not isinstance(payload["segment_id"], str) or not isinstance(
            payload["schema_version"], str
        ):
            raise ReviewValidationError("manifest metadata must be strings")
        manifest = cls(
            segment_id=payload["segment_id"],
            cases=tuple(cases),
            schema_version=payload["schema_version"],
        )
        if manifest.to_bytes() != raw:
            raise ReviewValidationError("manifest is not canonical")
        return manifest


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    case_id: str
    content_sha256: str
    review_status: ReviewStatus
    critical_field_review: Literal["pending", "verified"]
    search_eligible: bool
    answer_eligible: bool
    critical_reviewer_id: str | None
    search_reviewer_id: str | None
    answer_reviewer_id: str | None
    content_verified: bool
    basis_verified: bool
    privacy_verified: bool
    version: int


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    event_id: str
    case_id: str
    action: str
    before_state: ReviewStatus
    after_state: ReviewStatus
    actor_id: str
    occurred_at: datetime
    reason: str
    reviewed_content_sha256: str
    batch_manifest_sha256: str | None


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    journal_mode: str
    foreign_keys: bool
    busy_timeout_ms: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    purpose: ReviewPurpose
    ready: bool
    total: int
    eligible: int
    blockers: dict[str, int]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReviewValidationError("canonical JSON contains duplicate fields")
        result[key] = value
    return result


def _validate_identifier(value: str, field: str) -> None:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ReviewValidationError(f"{field} is invalid")


def _validate_case_id(value: str) -> None:
    try:
        validate_case_id(value)
    except ValueError as error:
        raise ReviewValidationError(
            "case_id must be an opaque canonical case ID"
        ) from error
    if _INVALID_DUPLICATE_PAGE.search(value) is not None:
        raise ReviewValidationError("case_id must be an opaque canonical case ID")


def _execute_schema_statements(connection: sqlite3.Connection, schema_sql: str) -> None:
    """Execute DDL without ``executescript``'s implicit transaction boundary."""
    statement_lines: list[str] = []
    for line in schema_sql.splitlines(keepends=True):
        statement_lines.append(line)
        candidate = "".join(statement_lines).strip()
        if candidate and sqlite3.complete_statement(candidate):
            connection.execute(candidate)
            statement_lines.clear()
    if "".join(statement_lines).strip():
        raise ReviewValidationError("review database schema is incomplete")


def _enable_wal_with_deadline(
    connection: sqlite3.Connection, *, busy_timeout_ms: int
) -> None:
    """Negotiate WAL despite SQLite's non-waiting journal-mode lock path."""
    original_busy_timeout_ms = int(
        connection.execute("PRAGMA busy_timeout").fetchone()[0]
    )
    deadline = time.monotonic() + (busy_timeout_ms / 1_000)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ReviewValidationError("review database WAL negotiation timed out")
            attempt_timeout_ms = max(
                1,
                min(
                    max(original_busy_timeout_ms, 1),
                    math.ceil(remaining * 1_000),
                ),
            )
            connection.execute(f"PRAGMA busy_timeout = {attempt_timeout_ms:d}")
            try:
                mode = str(
                    connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                )
            except sqlite3.OperationalError as error:
                error_code = getattr(error, "sqlite_errorcode", None)
                primary_code = (
                    error_code & 0xFF if isinstance(error_code, int) else None
                )
                if primary_code not in {
                    sqlite3.SQLITE_BUSY,
                    sqlite3.SQLITE_LOCKED,
                }:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.01, remaining))
                continue
            if mode != "wal":
                raise ReviewValidationError("review database WAL mode is unavailable")
            return
    finally:
        connection.execute(f"PRAGMA busy_timeout = {original_busy_timeout_ms:d}")


def _source_location_key(
    location: ReviewSourceLocation,
) -> tuple[int, tuple[float, float, float, float], str, int]:
    return (location.page_id, location.bbox, location.reason_code, location.count)


def _validate_provenance_reason(value: str) -> None:
    if value not in _PROVENANCE_REASON_CODES:
        raise ReviewValidationError(
            "provenance reason must be an allowlisted review code"
        )


def validate_review_reason(value: str) -> str:
    """Return a finite audit reason code without reflecting rejected input."""
    if value not in _REVIEW_REASON_CODES:
        raise ReviewValidationError("reason must be an allowlisted code")
    return value


def _validate_reason(value: str) -> None:
    validate_review_reason(value)


def _validate_hash(value: str, field: str) -> None:
    if _HASH_PATTERN.fullmatch(value) is None:
        raise ReviewValidationError(f"{field} must be lowercase SHA-256")


def _same_reviewer_identity(first: str | None, second: str) -> bool:
    if first is None:
        return False
    if first == second:
        return True
    first_uid = _OS_ACTOR_PATTERN.match(first)
    second_uid = _OS_ACTOR_PATTERN.match(second)
    return (
        first_uid is not None
        and second_uid is not None
        and int(first_uid.group(1)) == int(second_uid.group(1))
    )


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ReviewValidationError("clock must return a timezone-aware datetime")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ReviewValidationError("stored audit timestamp is not timezone aware")
    return parsed.astimezone(UTC)


class ReviewStore:
    """Versioned SQLite review state isolated from canonical candidate content."""

    def __init__(
        self,
        path: Path,
        *,
        canonical_registry: VerifiedCanonicalReviewRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms < 5_000:
            raise ReviewValidationError(
                "busy timeout must be at least 5000 milliseconds"
            )
        self.path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed = False
        self._write_authorized = False
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            path,
            isolation_level=None,
            timeout=busy_timeout_ms / 1_000,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function(
            "review_write_authorized", 0, lambda: int(self._write_authorized)
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
        try:
            self._initialize_schema(canonical_registry)
            _enable_wal_with_deadline(self._connection, busy_timeout_ms=busy_timeout_ms)
            settings = self.database_settings()
            if (
                settings.journal_mode != "wal"
                or not settings.foreign_keys
                or settings.busy_timeout_ms < 5_000
                or settings.schema_version != _SCHEMA_VERSION
            ):
                raise ReviewValidationError(
                    "review database safety settings are unavailable"
                )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _initialize_schema(
        self, canonical_registry: VerifiedCanonicalReviewRegistry | None
    ) -> None:
        if canonical_registry is not None and not isinstance(
            canonical_registry, VerifiedCanonicalReviewRegistry
        ):
            raise ReviewValidationError(
                "fresh store requires a verified canonical registry"
            )
        self._write_authorized = True
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, _SCHEMA_VERSION}:
                raise ReviewValidationError(
                    "unsupported review database schema version"
                )
            if current == 0:
                if canonical_registry is None:
                    raise ReviewValidationError(
                        "fresh review database requires a canonical registry"
                    )
                _execute_schema_statements(self._connection, _REVIEW_SCHEMA_SQL)
                applied_at = _format_utc(self._clock())
                self._connection.execute(
                    "INSERT INTO review_schema_migrations(schema_version, applied_at) VALUES (?, ?)",
                    (_SCHEMA_VERSION, applied_at),
                )
                fingerprint = canonical_registry.fingerprint_sha256
                self._connection.execute(
                    """
                    INSERT INTO review_registry_meta(
                        singleton, schema_version, fingerprint_sha256, loaded_at
                    ) VALUES (1, ?, ?, ?)
                    """,
                    (canonical_registry.schema_version, fingerprint, applied_at),
                )
                for reference in canonical_registry.cases:
                    self._connection.execute(
                        """
                        INSERT INTO canonical_review_bindings(
                            case_id, content_sha256, registry_sha256
                        ) VALUES (?, ?, ?)
                        """,
                        (reference.case_id, reference.content_sha256, fingerprint),
                    )
                    for sequence, location in enumerate(
                        reference.source_locations, start=1
                    ):
                        self._connection.execute(
                            """
                            INSERT INTO canonical_review_locations(
                                case_id, sequence, page_id, x0, y0, x1, y1,
                                reason_code, finding_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                reference.case_id,
                                sequence,
                                location.page_id,
                                *location.bbox,
                                location.reason_code,
                                location.count,
                            ),
                        )
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION:d}")

            row = self._connection.execute(
                "SELECT schema_version, fingerprint_sha256 FROM review_registry_meta WHERE singleton=1"
            ).fetchone()
            if row is None or str(row["schema_version"]) != _REGISTRY_SCHEMA:
                raise ReviewValidationError(
                    "canonical registry metadata is unavailable"
                )
            if (
                canonical_registry is not None
                and str(row["fingerprint_sha256"])
                != canonical_registry.fingerprint_sha256
            ):
                raise ReviewConflictError("canonical registry fingerprint mismatch")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        finally:
            self._write_authorized = False

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._connection.in_transaction or self._write_authorized:
            raise ReviewConflictError("nested review transaction is not allowed")
        self._write_authorized = True
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")
        finally:
            self._write_authorized = False

    def database_settings(self) -> DatabaseSettings:
        journal_mode = str(
            self._connection.execute("PRAGMA journal_mode").fetchone()[0]
        )
        foreign_keys = bool(
            self._connection.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        busy_timeout = int(
            self._connection.execute("PRAGMA busy_timeout").fetchone()[0]
        )
        schema_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        return DatabaseSettings(
            journal_mode, foreign_keys, busy_timeout, schema_version
        )

    def register_candidate(self, case_id: str, *, content_sha256: str) -> None:
        """Register a content address in the initial machine state, without candidate text."""
        _validate_case_id(case_id)
        _validate_hash(content_sha256, "content_sha256")
        now = _format_utc(self._clock())
        with self._transaction():
            self._assert_canonical_binding(case_id, content_sha256)
            if self._connection.execute(
                "SELECT 1 FROM review_cases WHERE case_id = ?", (case_id,)
            ).fetchone():
                raise ReviewConflictError("case is already registered")
            self._connection.execute(
                """
                INSERT INTO review_cases(
                    case_id, content_sha256, review_status, critical_field_review,
                    search_eligible, answer_eligible, critical_reviewer_id,
                    search_reviewer_id, answer_reviewer_id, content_verified,
                    basis_verified, privacy_verified, queued_at, updated_at, version
                ) VALUES (?, ?, 'machine_extracted', 'pending', 0, 0, NULL, NULL,
                          NULL, 0, 0, 0, ?, ?, 0)
                """,
                (case_id, content_sha256, now, now),
            )

    def enqueue(
        self,
        case_id: str,
        *,
        content_sha256: str,
        reason: str,
        actor_id: str = "quality-gate",
        event_id: str | None = None,
    ) -> None:
        """Atomically register and place one quality-gated candidate in review."""
        _validate_case_id(case_id)
        _validate_hash(content_sha256, "content_sha256")
        _validate_identifier(actor_id, "actor_id")
        _validate_reason(reason)
        now = _format_utc(self._clock())
        with self._transaction():
            self._assert_canonical_binding(case_id, content_sha256)
            if self._connection.execute(
                "SELECT 1 FROM review_cases WHERE case_id = ?", (case_id,)
            ).fetchone():
                raise ReviewConflictError("case is already registered")
            self._connection.execute(
                """
                INSERT INTO review_cases(
                    case_id, content_sha256, review_status, critical_field_review,
                    search_eligible, answer_eligible, critical_reviewer_id,
                    search_reviewer_id, answer_reviewer_id, content_verified,
                    basis_verified, privacy_verified, queued_at, updated_at, version
                ) VALUES (?, ?, 'machine_extracted', 'pending', 0, 0, NULL, NULL,
                          NULL, 0, 0, 0, ?, ?, 0)
                """,
                (case_id, content_sha256, now, now),
            )
            self._append_event(
                case_id=case_id,
                action="enqueue",
                before_state="machine_extracted",
                after_state="needs_review",
                actor_id=actor_id,
                reason=reason,
                reviewed_content_sha256=content_sha256,
                batch_manifest_sha256=None,
                event_id=event_id,
                occurred_at=now,
            )
            self._connection.execute(
                """
                UPDATE review_cases
                SET review_status = 'needs_review', updated_at = ?, version = 1
                WHERE case_id = ?
                """,
                (now, case_id),
            )

    def mark_needs_review(
        self,
        case_id: str,
        *,
        reason: str,
        actor_id: str = "quality-gate",
        reviewed_content_sha256: str | None = None,
        event_id: str | None = None,
    ) -> None:
        """Move a previously registered machine candidate into the quality queue."""
        _validate_identifier(actor_id, "actor_id")
        _validate_reason(reason)
        with self._transaction():
            record = self._load(case_id)
            supplied_hash = reviewed_content_sha256 or record.content_sha256
            self._check_hash(record, supplied_hash)
            self._check_state(record, "machine_extracted")
            now = _format_utc(self._clock())
            self._append_event(
                case_id=case_id,
                action="enqueue",
                before_state="machine_extracted",
                after_state="needs_review",
                actor_id=actor_id,
                reason=reason,
                reviewed_content_sha256=supplied_hash,
                batch_manifest_sha256=None,
                event_id=event_id,
                occurred_at=now,
            )
            self._connection.execute(
                "UPDATE review_cases SET review_status='needs_review', updated_at=?, version=version+1 WHERE case_id=?",
                (now, case_id),
            )

    def get(self, case_id: str) -> ReviewRecord:
        return self._load(case_id)

    def canonical_reference(self, case_id: str) -> ReviewReference:
        """Return the value-free hash and provenance bound at database creation."""
        _validate_case_id(case_id)
        binding = self._connection.execute(
            "SELECT content_sha256 FROM canonical_review_bindings WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if binding is None:
            raise ReviewNotFoundError("case is not in canonical registry")
        rows = self._connection.execute(
            """
            SELECT page_id, x0, y0, x1, y1, reason_code, finding_count
            FROM canonical_review_locations
            WHERE case_id = ?
            ORDER BY sequence
            """,
            (case_id,),
        ).fetchall()
        if not rows:
            raise ReviewValidationError("canonical source provenance is unavailable")
        locations = tuple(
            ReviewSourceLocation(
                page_id=int(row["page_id"]),
                bbox=(
                    float(row["x0"]),
                    float(row["y0"]),
                    float(row["x1"]),
                    float(row["y1"]),
                ),
                reason_code=cast(
                    ReviewProvenanceReason,
                    str(row["reason_code"]),
                ),
                count=int(row["finding_count"]),
            )
            for row in rows
        )
        return ReviewReference(
            case_id=case_id,
            content_sha256=str(binding["content_sha256"]),
            source_locations=locations,
        )

    def _load(self, case_id: str) -> ReviewRecord:
        _validate_case_id(case_id)
        row = self._connection.execute(
            "SELECT * FROM review_cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            raise ReviewNotFoundError("case is not in review queue")
        return _record_from_row(row)

    def _assert_candidate_approvable(self, case_id: str) -> None:
        restricted = self._connection.execute(
            """
            SELECT 1
            FROM canonical_review_locations
            WHERE case_id = ? AND reason_code = 'restricted-pii'
            LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        if restricted is not None:
            raise ReviewValidationError(_RESTRICTED_APPROVAL_ERROR) from None

    def events(self, case_id: str) -> tuple[ReviewEvent, ...]:
        _validate_case_id(case_id)
        rows = self._connection.execute(
            "SELECT * FROM review_events WHERE case_id = ? ORDER BY event_sequence",
            (case_id,),
        ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def export_decision_snapshot(self) -> bytes:
        """Export one canonical terminal decision view from a single DB snapshot."""
        if self._connection.in_transaction:
            raise ReviewConflictError("review snapshot cannot nest a transaction")
        failure: str | None = None
        rendered: bytes | None = None
        try:
            self._connection.execute("BEGIN")
            registry_row = self._connection.execute(
                """
                SELECT fingerprint_sha256
                FROM review_registry_meta
                WHERE singleton = 1
                """
            ).fetchone()
            rows = self._connection.execute(
                """
                SELECT
                    binding.case_id AS binding_case_id,
                    binding.content_sha256 AS binding_content_sha256,
                    decision.*
                FROM canonical_review_bindings AS binding
                LEFT JOIN review_cases AS decision
                    ON decision.case_id = binding.case_id
                    AND decision.content_sha256 = binding.content_sha256
                ORDER BY binding.case_id
                LIMIT ?
                """,
                (_MAX_DECISION_SNAPSHOT_CASES + 1,),
            ).fetchall()
            if len(rows) > _MAX_DECISION_SNAPSHOT_CASES:
                raise ReviewValidationError(_SNAPSHOT_BOUNDED_ERROR)
            event_rows = self._connection.execute(
                """
                SELECT *
                FROM review_events
                ORDER BY case_id, event_sequence
                LIMIT ?
                """,
                (_MAX_DECISION_SNAPSHOT_EVENTS + 1,),
            ).fetchall()
            if len(event_rows) > _MAX_DECISION_SNAPSHOT_EVENTS:
                raise ReviewValidationError(_SNAPSHOT_BOUNDED_ERROR)

            case_ids = tuple(str(row["binding_case_id"]) for row in rows)
            if (
                registry_row is None
                or not rows
                or any(
                    row["case_id"] is None
                    or row["review_status"]
                    not in {"search_approved", "approved", "rejected"}
                    for row in rows
                )
            ):
                raise ReviewValidationError(_SNAPSHOT_COMPLETE_ERROR)

            events_by_case: dict[str, list[dict[str, object]]] = {
                case_id: [] for case_id in case_ids
            }
            for event_row in event_rows:
                case_id = str(event_row["case_id"])
                if case_id not in events_by_case:
                    raise ReviewValidationError(_SNAPSHOT_COMPLETE_ERROR)
                events = events_by_case[case_id]
                events.append(
                    {
                        "action": str(event_row["action"]),
                        "actor_id": str(event_row["actor_id"]),
                        "after_state": str(event_row["after_state"]),
                        "batch_manifest_sha256": cast(
                            str | None,
                            event_row["batch_manifest_sha256"],
                        ),
                        "before_state": str(event_row["before_state"]),
                        "event_id": str(event_row["event_id"]),
                        "event_sequence": len(events) + 1,
                        "occurred_at": str(event_row["occurred_at"]),
                        "reason": str(event_row["reason"]),
                        "reviewed_content_sha256": str(
                            event_row["reviewed_content_sha256"]
                        ),
                    }
                )

            cases: list[dict[str, object]] = []
            for row in rows:
                case_id = str(row["binding_case_id"])
                events = events_by_case[case_id]
                if int(row["version"]) != len(events) or not events:
                    raise ReviewValidationError(_SNAPSHOT_COMPLETE_ERROR)
                cases.append(
                    {
                        "case_id": case_id,
                        "corrections": [],
                        "events": events,
                        "promotion_envelope_sha256": str(row["binding_content_sha256"]),
                        "review_record": {
                            "answer_eligible": bool(row["answer_eligible"]),
                            "answer_reviewer_id": cast(
                                str | None,
                                row["answer_reviewer_id"],
                            ),
                            "basis_verified": bool(row["basis_verified"]),
                            "content_sha256": str(row["content_sha256"]),
                            "content_verified": bool(row["content_verified"]),
                            "critical_field_review": str(row["critical_field_review"]),
                            "critical_reviewer_id": cast(
                                str | None,
                                row["critical_reviewer_id"],
                            ),
                            "privacy_verified": bool(row["privacy_verified"]),
                            "review_status": str(row["review_status"]),
                            "search_eligible": bool(row["search_eligible"]),
                            "search_reviewer_id": cast(
                                str | None,
                                row["search_reviewer_id"],
                            ),
                            "version": int(row["version"]),
                        },
                    }
                )
            payload = {
                "cases": cases,
                "registry_fingerprint_sha256": str(registry_row["fingerprint_sha256"]),
                "schema_version": _DECISION_SNAPSHOT_SCHEMA,
            }
            rendered = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            if len(rendered) > _MAX_DECISION_SNAPSHOT_BYTES:
                raise ReviewValidationError(_SNAPSHOT_BOUNDED_ERROR)
        except ReviewValidationError as error:
            candidate = str(error)
            failure = (
                candidate
                if candidate in {_SNAPSHOT_BOUNDED_ERROR, _SNAPSHOT_COMPLETE_ERROR}
                else _SNAPSHOT_INVALID_ERROR
            )
        except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
            failure = _SNAPSHOT_INVALID_ERROR
        finally:
            if self._connection.in_transaction:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    failure = _SNAPSHOT_INVALID_ERROR
        if failure is not None:
            raise ReviewValidationError(failure) from None
        if rendered is None:
            raise ReviewValidationError(_SNAPSHOT_INVALID_ERROR) from None
        return rendered

    def verify_critical_fields(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus = "needs_review",
        event_id: str | None = None,
    ) -> None:
        self._validate_action_input(reviewer_id, reviewed_content_sha256, reason)
        with self._transaction():
            self._verify_fields_tx(
                case_id,
                reviewer_id=reviewer_id,
                reviewed_content_sha256=reviewed_content_sha256,
                reason=reason,
                expected_state=expected_state,
                event_id=event_id,
                batch_manifest_sha256=None,
            )

    def _verify_fields_tx(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus,
        event_id: str | None,
        batch_manifest_sha256: str | None,
    ) -> None:
        record = self._load(case_id)
        self._check_hash(record, reviewed_content_sha256)
        self._check_state(record, expected_state)
        if expected_state != "needs_review":
            raise ReviewConflictError("stale expected state")
        if record.critical_field_review == "verified":
            raise ReviewConflictError("no-op verification is not allowed")
        now = _format_utc(self._clock())
        self._append_event(
            case_id=case_id,
            action="verify_fields",
            before_state=record.review_status,
            after_state=record.review_status,
            actor_id=reviewer_id,
            reason=reason,
            reviewed_content_sha256=reviewed_content_sha256,
            batch_manifest_sha256=batch_manifest_sha256,
            event_id=event_id,
            occurred_at=now,
        )
        self._connection.execute(
            """
            UPDATE review_cases
            SET critical_field_review='verified', critical_reviewer_id=?,
                updated_at=?, version=version+1
            WHERE case_id=?
            """,
            (reviewer_id, now, case_id),
        )

    def approve_search(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus = "needs_review",
        event_id: str | None = None,
    ) -> None:
        self._validate_action_input(reviewer_id, reviewed_content_sha256, reason)
        with self._transaction():
            self._approve_search_tx(
                case_id,
                reviewer_id=reviewer_id,
                reviewed_content_sha256=reviewed_content_sha256,
                reason=reason,
                expected_state=expected_state,
                event_id=event_id,
                batch_manifest_sha256=None,
            )

    def _approve_search_tx(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus,
        event_id: str | None,
        batch_manifest_sha256: str | None,
    ) -> None:
        record = self._load(case_id)
        self._check_hash(record, reviewed_content_sha256)
        self._check_state(record, expected_state)
        if expected_state != "needs_review":
            raise ReviewConflictError("stale expected state")
        if record.critical_field_review != "verified":
            raise ReviewValidationError("critical fields must be verified")
        self._assert_candidate_approvable(case_id)
        now = _format_utc(self._clock())
        self._append_event(
            case_id=case_id,
            action="approve_search",
            before_state="needs_review",
            after_state="search_approved",
            actor_id=reviewer_id,
            reason=reason,
            reviewed_content_sha256=reviewed_content_sha256,
            batch_manifest_sha256=batch_manifest_sha256,
            event_id=event_id,
            occurred_at=now,
        )
        self._connection.execute(
            """
            UPDATE review_cases
            SET review_status='search_approved', search_eligible=1,
                answer_eligible=0, search_reviewer_id=?, updated_at=?, version=version+1
            WHERE case_id=?
            """,
            (reviewer_id, now, case_id),
        )

    def approve_answer(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        content_verified: bool,
        basis_verified: bool,
        privacy_verified: bool,
        expected_state: ReviewStatus = "search_approved",
        event_id: str | None = None,
    ) -> None:
        self._validate_action_input(reviewer_id, reviewed_content_sha256, reason)
        if not all((content_verified, basis_verified, privacy_verified)):
            raise ReviewValidationError(
                "answer approval requires all three verification flags"
            )
        with self._transaction():
            self._approve_answer_tx(
                case_id,
                reviewer_id=reviewer_id,
                reviewed_content_sha256=reviewed_content_sha256,
                reason=reason,
                expected_state=expected_state,
                event_id=event_id,
                batch_manifest_sha256=None,
            )

    def _approve_answer_tx(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus,
        event_id: str | None,
        batch_manifest_sha256: str | None,
    ) -> None:
        record = self._load(case_id)
        self._check_hash(record, reviewed_content_sha256)
        self._check_state(record, expected_state)
        if expected_state != "search_approved":
            raise ReviewConflictError("stale expected state")
        self._assert_candidate_approvable(case_id)
        if any(
            _same_reviewer_identity(prior, reviewer_id)
            for prior in (record.critical_reviewer_id, record.search_reviewer_id)
        ):
            raise ReviewValidationError(
                "answer approval requires an independent reviewer"
            )
        now = _format_utc(self._clock())
        self._append_event(
            case_id=case_id,
            action="approve_answer",
            before_state="search_approved",
            after_state="approved",
            actor_id=reviewer_id,
            reason=reason,
            reviewed_content_sha256=reviewed_content_sha256,
            batch_manifest_sha256=batch_manifest_sha256,
            event_id=event_id,
            occurred_at=now,
        )
        self._connection.execute(
            """
            UPDATE review_cases
            SET review_status='approved', search_eligible=1, answer_eligible=1,
                answer_reviewer_id=?, content_verified=1, basis_verified=1,
                privacy_verified=1, updated_at=?, version=version+1
            WHERE case_id=?
            """,
            (reviewer_id, now, case_id),
        )

    def reject(
        self,
        case_id: str,
        *,
        reviewer_id: str,
        reviewed_content_sha256: str,
        reason: str,
        expected_state: ReviewStatus | None = None,
        event_id: str | None = None,
    ) -> None:
        self._validate_action_input(reviewer_id, reviewed_content_sha256, reason)
        with self._transaction():
            record = self._load(case_id)
            self._check_hash(record, reviewed_content_sha256)
            if expected_state is not None:
                self._check_state(record, expected_state)
            if record.review_status in _TERMINAL_STATES:
                raise ReviewConflictError("terminal review state cannot transition")
            now = _format_utc(self._clock())
            self._append_event(
                case_id=case_id,
                action="reject",
                before_state=record.review_status,
                after_state="rejected",
                actor_id=reviewer_id,
                reason=reason,
                reviewed_content_sha256=reviewed_content_sha256,
                batch_manifest_sha256=None,
                event_id=event_id,
                occurred_at=now,
            )
            self._connection.execute(
                """
                UPDATE review_cases
                SET review_status='rejected', search_eligible=0, answer_eligible=0,
                    updated_at=?, version=version+1 WHERE case_id=?
                """,
                (now, case_id),
            )

    def run_mode(
        self,
        mode: RunMode,
        *,
        cases: Iterable[ReviewReference],
        reviewer_id: str,
        reason: str,
        content_verified: bool = False,
        basis_verified: bool = False,
        privacy_verified: bool = False,
        manifest_sha256: str | None = None,
    ) -> int:
        self._validate_action_input(reviewer_id, "0" * 64, reason)
        references = self._validate_references(cases)
        if mode not in {"critical-fields-all", "answer-and-basis-all"}:
            raise ReviewValidationError("unsupported review run mode")
        if mode == "answer-and-basis-all" and not all(
            (content_verified, basis_verified, privacy_verified)
        ):
            raise ReviewValidationError(
                "answer approval requires all three verification flags"
            )
        if manifest_sha256 is not None:
            _validate_hash(manifest_sha256, "manifest_sha256")
        with self._transaction():
            records = [self._load(reference.case_id) for reference in references]
            for record, reference in zip(records, references, strict=True):
                self._check_hash(record, reference.content_sha256)
                required = (
                    "needs_review"
                    if mode == "critical-fields-all"
                    else "search_approved"
                )
                self._check_state(record, cast(ReviewStatus, required))
                if (
                    mode == "critical-fields-all"
                    and record.critical_field_review != "pending"
                ):
                    raise ReviewConflictError("no-op verification is not allowed")
                if mode == "answer-and-basis-all" and any(
                    _same_reviewer_identity(prior, reviewer_id)
                    for prior in (
                        record.critical_reviewer_id,
                        record.search_reviewer_id,
                    )
                ):
                    raise ReviewValidationError(
                        "answer approval requires an independent reviewer"
                    )
            for reference in references:
                if mode == "critical-fields-all":
                    self._verify_fields_tx(
                        reference.case_id,
                        reviewer_id=reviewer_id,
                        reviewed_content_sha256=reference.content_sha256,
                        reason=reason,
                        expected_state="needs_review",
                        event_id=None,
                        batch_manifest_sha256=manifest_sha256,
                    )
                    self._approve_search_tx(
                        reference.case_id,
                        reviewer_id=reviewer_id,
                        reviewed_content_sha256=reference.content_sha256,
                        reason=reason,
                        expected_state="needs_review",
                        event_id=None,
                        batch_manifest_sha256=manifest_sha256,
                    )
                else:
                    self._approve_answer_tx(
                        reference.case_id,
                        reviewer_id=reviewer_id,
                        reviewed_content_sha256=reference.content_sha256,
                        reason=reason,
                        expected_state="search_approved",
                        event_id=None,
                        batch_manifest_sha256=manifest_sha256,
                    )
        return len(references)

    def run_case_complete(
        self,
        mode: RunMode,
        *,
        reference: ReviewReference,
        manifest_sha256: str,
    ) -> bool:
        """Return whether this exact manifest already completed one run case."""
        if mode not in {"critical-fields-all", "answer-and-basis-all"}:
            raise ReviewValidationError("unsupported review run mode")
        _validate_hash(manifest_sha256, "manifest_sha256")
        record = self._load(reference.case_id)
        self._check_hash(record, reference.content_sha256)
        if mode == "critical-fields-all":
            if not record.search_eligible or record.review_status not in {
                "search_approved",
                "approved",
            }:
                return False
            required_actions = {"verify_fields", "approve_search"}
        else:
            if not record.answer_eligible or record.review_status != "approved":
                return False
            required_actions = {"approve_answer"}
        rows = self._connection.execute(
            """
            SELECT DISTINCT action
            FROM review_events
            WHERE case_id = ? AND batch_manifest_sha256 = ?
            """,
            (reference.case_id, manifest_sha256),
        ).fetchall()
        completed_actions = {str(row["action"]) for row in rows}
        return required_actions <= completed_actions

    def approve_search_batch(
        self,
        manifest_bytes: bytes,
        *,
        manifest_sha256: str,
        reviewer_id: str,
        reason: str,
    ) -> int:
        self._validate_action_input(reviewer_id, manifest_sha256, reason)
        actual_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if actual_hash != manifest_sha256:
            raise ReviewValidationError("manifest hash mismatch")
        manifest = SegmentManifest.from_bytes(manifest_bytes)
        with self._transaction():
            if self._connection.execute(
                "SELECT 1 FROM review_batch_manifests WHERE manifest_sha256=?",
                (manifest_sha256,),
            ).fetchone():
                raise ReviewConflictError("batch manifest replay is not allowed")
            records = [self._load(reference.case_id) for reference in manifest.cases]
            for record, reference in zip(records, manifest.cases, strict=True):
                self._check_hash(record, reference.content_sha256)
                self._check_state(record, "needs_review")
                if record.critical_field_review != "verified":
                    raise ReviewValidationError("critical fields must be verified")
            now = _format_utc(self._clock())
            self._connection.execute(
                """
                INSERT INTO review_batch_manifests(
                    manifest_sha256, segment_id, actor_id, approved_at, case_count
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    manifest_sha256,
                    manifest.segment_id,
                    reviewer_id,
                    now,
                    len(manifest.cases),
                ),
            )
            for reference in manifest.cases:
                event_id = hashlib.sha256(
                    f"{manifest_sha256}:{reference.case_id}:approve_search".encode()
                ).hexdigest()
                self._approve_search_tx(
                    reference.case_id,
                    reviewer_id=reviewer_id,
                    reviewed_content_sha256=reference.content_sha256,
                    reason=reason,
                    expected_state="needs_review",
                    event_id=event_id,
                    batch_manifest_sha256=manifest_sha256,
                )
        return len(manifest.cases)

    def assert_ready(self, *, purpose: ReviewPurpose = "answer") -> ReadinessReport:
        if purpose not in {"search", "answer"}:
            raise ReviewValidationError("purpose must be search or answer")
        eligibility_column = (
            "search_eligible" if purpose == "search" else "answer_eligible"
        )
        rows = self._connection.execute(
            f"""
            WITH summary AS (
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM({eligibility_column}), 0) AS eligible
                FROM review_cases
            ), blockers AS (
                SELECT review_status, COUNT(*) AS blocker_count
                FROM review_cases
                WHERE {eligibility_column} = 0
                GROUP BY review_status
            )
            SELECT
                summary.total,
                summary.eligible,
                blockers.review_status,
                blockers.blocker_count
            FROM summary
            LEFT JOIN blockers ON 1 = 1
            ORDER BY blockers.review_status
            """
        ).fetchall()
        if not rows:
            raise ReviewValidationError("review readiness snapshot is unavailable")
        total = int(rows[0]["total"])
        eligible = int(rows[0]["eligible"])
        blockers = {
            str(row["review_status"]): int(row["blocker_count"])
            for row in rows
            if row["review_status"] is not None
        }
        return ReadinessReport(
            purpose=purpose,
            ready=total > 0 and eligible == total,
            total=total,
            eligible=eligible,
            blockers=blockers,
        )

    def _validate_action_input(
        self, reviewer_id: str, reviewed_content_sha256: str, reason: str
    ) -> None:
        _validate_identifier(reviewer_id, "reviewer_id")
        _validate_hash(reviewed_content_sha256, "reviewed_content_sha256")
        _validate_reason(reason)

    def _assert_canonical_binding(self, case_id: str, content_sha256: str) -> None:
        row = self._connection.execute(
            "SELECT content_sha256 FROM canonical_review_bindings WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise ReviewNotFoundError("case is not in canonical registry")
        if str(row["content_sha256"]) != content_sha256:
            raise ReviewConflictError("canonical content binding mismatch")

    def _validate_references(
        self, cases: Iterable[ReviewReference]
    ) -> tuple[ReviewReference, ...]:
        references = tuple(sorted(cases, key=lambda item: item.case_id))
        if not references:
            raise ReviewValidationError("review run requires at least one case")
        if len({reference.case_id for reference in references}) != len(references):
            raise ReviewValidationError("review run contains duplicate cases")
        return references

    @staticmethod
    def _check_hash(record: ReviewRecord, reviewed_content_sha256: str) -> None:
        _validate_hash(reviewed_content_sha256, "reviewed_content_sha256")
        if record.content_sha256 != reviewed_content_sha256:
            raise ReviewConflictError("content hash drift detected")

    @staticmethod
    def _check_state(record: ReviewRecord, expected_state: ReviewStatus) -> None:
        if record.review_status in _TERMINAL_STATES:
            raise ReviewConflictError("terminal review state cannot transition")
        if record.review_status != expected_state:
            raise ReviewConflictError("stale expected state")

    def _append_event(
        self,
        *,
        case_id: str,
        action: str,
        before_state: ReviewStatus,
        after_state: ReviewStatus,
        actor_id: str,
        reason: str,
        reviewed_content_sha256: str,
        batch_manifest_sha256: str | None,
        event_id: str | None,
        occurred_at: str,
    ) -> None:
        resolved_event_id = event_id or uuid.uuid4().hex
        _validate_identifier(resolved_event_id, "event_id")
        if self._connection.execute(
            "SELECT 1 FROM review_events WHERE event_id=?", (resolved_event_id,)
        ).fetchone():
            raise ReviewConflictError("duplicate event replay is not allowed")
        self._connection.execute(
            """
            INSERT INTO review_events(
                event_id, case_id, action, before_state, after_state, actor_id,
                occurred_at, reason, reviewed_content_sha256, batch_manifest_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_event_id,
                case_id,
                action,
                before_state,
                after_state,
                actor_id,
                occurred_at,
                reason,
                reviewed_content_sha256,
                batch_manifest_sha256,
            ),
        )


def _record_from_row(row: sqlite3.Row) -> ReviewRecord:
    return ReviewRecord(
        case_id=str(row["case_id"]),
        content_sha256=str(row["content_sha256"]),
        review_status=cast(ReviewStatus, row["review_status"]),
        critical_field_review=cast(
            Literal["pending", "verified"], row["critical_field_review"]
        ),
        search_eligible=bool(row["search_eligible"]),
        answer_eligible=bool(row["answer_eligible"]),
        critical_reviewer_id=cast(str | None, row["critical_reviewer_id"]),
        search_reviewer_id=cast(str | None, row["search_reviewer_id"]),
        answer_reviewer_id=cast(str | None, row["answer_reviewer_id"]),
        content_verified=bool(row["content_verified"]),
        basis_verified=bool(row["basis_verified"]),
        privacy_verified=bool(row["privacy_verified"]),
        version=int(row["version"]),
    )


def _event_from_row(row: sqlite3.Row) -> ReviewEvent:
    return ReviewEvent(
        event_id=str(row["event_id"]),
        case_id=str(row["case_id"]),
        action=str(row["action"]),
        before_state=cast(ReviewStatus, row["before_state"]),
        after_state=cast(ReviewStatus, row["after_state"]),
        actor_id=str(row["actor_id"]),
        occurred_at=_parse_utc(str(row["occurred_at"])),
        reason=str(row["reason"]),
        reviewed_content_sha256=str(row["reviewed_content_sha256"]),
        batch_manifest_sha256=cast(str | None, row["batch_manifest_sha256"]),
    )
