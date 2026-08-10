import re
from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_RFC3339_TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_SNAPSHOT_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_NAMESPACED_ID_PATTERN = re.compile(
    r"[a-z][a-z0-9-]{0,31}:"
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
    r"(?::[A-Za-z0-9][A-Za-z0-9_-]{0,63})*"
)


class InstitutionStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TEMPORARILY_CLOSED = "TEMPORARILY_CLOSED"
    CLOSED = "CLOSED"
    MISSING_FROM_SOURCE = "MISSING_FROM_SOURCE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class _StrictSnapshotModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        strict=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class Institution(_StrictSnapshotModel):
    institution_id: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    status: InstitutionStatus
    status_source: str
    effective_from: str
    effective_to: str | None
    last_seen_snapshot: str
    aliases: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    merged_into: str | None = None
    source: str
    source_region_code: str
    source_as_of: str

    @field_validator(
        "institution_id",
        "official_name",
        "institution_type",
        "foundation_type",
        "status_source",
        "effective_from",
        "last_seen_snapshot",
        "source",
        "source_region_code",
        "source_as_of",
    )
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("education_office", "effective_to", "merged_into")
    @classmethod
    def optional_strings_are_nonblank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonblank(value)

    @field_validator("aliases", "supersedes")
    @classmethod
    def string_tuples_contain_no_blanks(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("string tuple values must be nonblank")
        return values

    @field_validator("institution_id")
    @classmethod
    def institution_id_is_safe_and_namespaced(cls, value: str) -> str:
        return _require_namespaced_id(value, "institution")

    @field_validator("supersedes")
    @classmethod
    def superseded_ids_are_safe_and_namespaced(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        for value in values:
            _require_namespaced_id(value, "institution")
        return values

    @field_validator("merged_into")
    @classmethod
    def merged_target_is_safe_and_namespaced(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None:
            _require_namespaced_id(value, "institution")
        return value

    @field_validator("last_seen_snapshot")
    @classmethod
    def last_seen_snapshot_is_safe(cls, value: str) -> str:
        return _require_snapshot_slug(value)

    @field_validator("effective_from", "source_as_of")
    @classmethod
    def required_dates_are_iso_dates(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("effective_to")
    @classmethod
    def optional_date_is_iso_date(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_iso_date(value)
        return value

    @model_validator(mode="after")
    def effective_interval_is_ordered(self) -> Self:
        if self.effective_to is not None and _parse_iso_date(
            self.effective_to
        ) < _parse_iso_date(self.effective_from):
            raise ValueError("effectiveTo must not precede effectiveFrom")
        return self


class InstitutionSite(_StrictSnapshotModel):
    site_id: str
    institution_id: str
    site_name: str
    road_address: str
    district: str
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    coordinate_quality: str
    routing_anchor_latitude: float = Field(ge=-90.0, le=90.0)
    routing_anchor_longitude: float = Field(ge=-180.0, le=180.0)
    is_default: bool
    status: InstitutionStatus
    effective_from: str
    effective_to: str | None

    @field_validator(
        "site_id",
        "institution_id",
        "site_name",
        "road_address",
        "district",
        "coordinate_quality",
        "effective_from",
    )
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("effective_to")
    @classmethod
    def optional_strings_are_nonblank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonblank(value)

    @field_validator("site_id")
    @classmethod
    def site_id_is_safe_and_namespaced(cls, value: str) -> str:
        return _require_namespaced_id(value, "site")

    @field_validator("institution_id")
    @classmethod
    def parent_id_is_safe_and_namespaced(cls, value: str) -> str:
        return _require_namespaced_id(value, "institution")

    @field_validator("effective_from")
    @classmethod
    def required_date_is_iso_date(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("effective_to")
    @classmethod
    def optional_date_is_iso_date(cls, value: str | None) -> str | None:
        if value is not None:
            _parse_iso_date(value)
        return value

    @model_validator(mode="after")
    def effective_interval_is_ordered(self) -> Self:
        if self.effective_to is not None and _parse_iso_date(
            self.effective_to
        ) < _parse_iso_date(self.effective_from):
            raise ValueError("effectiveTo must not precede effectiveFrom")
        return self


class InstitutionSearchItem(_StrictSnapshotModel):
    institution_id: str
    site_id: str
    site_name: str
    official_name: str
    institution_type: str
    foundation_type: str
    education_office: str | None
    road_address: str
    district: str
    coordinate_quality: str
    snapshot_id: str
    snapshot_as_of: str


class SourceSnapshotInfo(_StrictSnapshotModel):
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str
    raw_sha256: str
    page_count: int = Field(ge=0)
    row_count: int = Field(ge=0)

    @field_validator(
        "source",
        "endpoint",
        "license_name",
        "attribution",
        "fetched_at",
        "source_as_of",
    )
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("raw_sha256")
    @classmethod
    def raw_hash_is_lowercase_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("source_as_of")
    @classmethod
    def source_date_is_iso_date(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("fetched_at")
    @classmethod
    def fetched_timestamp_is_timezone_aware(cls, value: str) -> str:
        _parse_rfc3339_timestamp(value)
        return value

    @model_validator(mode="after")
    def source_date_is_not_after_fetch(self) -> Self:
        if _parse_iso_date(self.source_as_of) > _parse_rfc3339_timestamp(
            self.fetched_at
        ).date():
            raise ValueError("sourceAsOf must not be later than fetchedAt date")
        return self


class SnapshotDiff(_StrictSnapshotModel):
    previous_snapshot_id: str | None
    added_count: int = Field(ge=0)
    changed_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    closed_candidate_count: int = Field(ge=0)

    @field_validator("previous_snapshot_id")
    @classmethod
    def previous_id_is_nonblank_when_present(cls, value: str | None) -> str | None:
        return None if value is None else _require_nonblank(value)

    @field_validator("previous_snapshot_id")
    @classmethod
    def previous_snapshot_id_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _require_snapshot_slug(value)


class SnapshotManifest(_StrictSnapshotModel):
    schema_version: int
    snapshot_id: str
    created_at: str
    snapshot_as_of: str
    approved: bool
    approved_at: str | None
    approved_by_role: str | None
    sources: tuple[SourceSnapshotInfo, ...]
    institutions_sha256: str
    sites_sha256: str
    institution_count: int = Field(ge=0)
    site_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    possible_match_count: int = Field(ge=0)
    counts_by_type: dict[str, int]
    counts_by_foundation: dict[str, int]
    counts_by_status: dict[str, int]
    coordinate_quality_counts: dict[str, int]
    diff: SnapshotDiff

    @field_validator("snapshot_id", "created_at", "snapshot_as_of")
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator("institutions_sha256", "sites_sha256")
    @classmethod
    def file_hash_is_lowercase_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator(
        "counts_by_type",
        "counts_by_foundation",
        "counts_by_status",
        "coordinate_quality_counts",
    )
    @classmethod
    def count_map_is_strict_and_nonnegative(cls, values: dict[str, int]) -> dict[str, int]:
        if any(not key.strip() for key in values):
            raise ValueError("count-map keys must be nonblank")
        if any(value < 0 for value in values.values()):
            raise ValueError("count-map values must be nonnegative")
        return values

    @model_validator(mode="after")
    def approval_is_complete(self) -> Self:
        if self.schema_version != 1:
            raise ValueError("schemaVersion must be 1")
        if not self.approved:
            raise ValueError("approved must be true")
        if self.approved_at is None or not self.approved_at.strip():
            raise ValueError("approvedAt must be nonblank")
        if self.approved_by_role is None or not self.approved_by_role.strip():
            raise ValueError("approvedByRole must be nonblank")
        if not self.sources:
            raise ValueError("sources must be nonempty")
        created_at = _parse_rfc3339_timestamp(self.created_at)
        approved_at = _parse_rfc3339_timestamp(self.approved_at)
        if created_at > approved_at:
            raise ValueError("createdAt must not be later than approvedAt")
        if _parse_iso_date(self.snapshot_as_of) > created_at.date():
            raise ValueError("snapshotAsOf must not be later than createdAt date")
        return self

    @field_validator("snapshot_as_of")
    @classmethod
    def snapshot_date_is_iso_date(cls, value: str) -> str:
        _parse_iso_date(value)
        return value

    @field_validator("created_at")
    @classmethod
    def creation_timestamp_is_timezone_aware(cls, value: str) -> str:
        _parse_rfc3339_timestamp(value)
        return value

    @field_validator("approved_at")
    @classmethod
    def approval_timestamp_is_timezone_aware(
        cls,
        value: str | None,
    ) -> str | None:
        if value is not None:
            _parse_rfc3339_timestamp(value)
        return value


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must be nonblank")
    return value


def _require_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _parse_iso_date(value: str) -> date:
    if _ISO_DATE_PATTERN.fullmatch(value) is None:
        raise ValueError("must be an ISO date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an ISO date in YYYY-MM-DD form") from exc


def _parse_rfc3339_timestamp(value: str) -> datetime:
    if _RFC3339_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a timezone-aware RFC3339 timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("must be a timezone-aware RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("must be a timezone-aware RFC3339 timestamp")
    return parsed


def _require_snapshot_slug(value: str) -> str:
    if _SNAPSHOT_SLUG_PATTERN.fullmatch(value) is None:
        raise ValueError("must be a bounded safe snapshot slug")
    return value


def _require_namespaced_id(value: str, entity: str) -> str:
    if len(value) > 255 or _NAMESPACED_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"must be a safe namespaced {entity} ID")
    return value
