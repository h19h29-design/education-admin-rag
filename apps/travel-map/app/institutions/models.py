import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self

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
_POPULATION_PROFILE_SHA256 = (
    "e904a254ab4f0fa264a0ec3894827e6bebbb2b94ab263bf635594c812dd7df06"
)
_SCHOOL_COUNT_BENCHMARK_SHA256 = (
    "36158d45a3b8c7e8a083e6d78f63fee706618f69eb49d8624877aef07e3a9332"
)
_SOURCE_CATEGORY_COUNTS = {
    "KINDERGARTEN_INFO": {"KINDERGARTEN_TOTAL": 706},
    "NEIS": {
        "각종학교(고)": 13,
        "각종학교(중)": 7,
        "각종학교(초)": 1,
        "고등기술학교": 1,
        "고등학교": 319,
        "공동실습소": 1,
        "방송통신고등학교": 5,
        "방송통신중학교": 1,
        "외국인학교": 17,
        "중학교": 390,
        "초등학교": 610,
        "특수학교": 32,
        "평생학교(고)-2년6학기": 7,
        "평생학교(고)-3년6학기": 4,
        "평생학교(중)-2년6학기": 5,
        "평생학교(초)-3년6학기": 2,
    },
}
_SOURCE_POPULATION_ROLE_COUNTS = {
    "KINDERGARTEN_INFO": {"BENCHMARK": 706},
    "NEIS": {
        "BENCHMARK": 1_373,
        "NONSELECTABLE": 1,
        "QUARANTINED": 18,
        "SUPPLEMENTARY": 23,
    },
}
_SCHOOL_COUNT_CATEGORY_RESULTS = {
    "ELEMENTARY_SCHOOL": (609, 610, 1),
    "HIGH_SCHOOL": (319, 319, 0),
    "KINDERGARTEN": (724, 706, -18),
    "MIDDLE_SCHOOL": (390, 390, 0),
    "MISC_SCHOOL": (18, 22, 4),
    "SPECIAL_SCHOOL": (32, 32, 0),
}


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


class _StrictManifestContractModel(_StrictSnapshotModel):
    model_config = ConfigDict(
        populate_by_name=False,
        validate_by_alias=True,
        validate_by_name=False,
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
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    coordinate_quality: str
    routing_anchor_latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    routing_anchor_longitude: float | None = Field(
        default=None,
        ge=-180.0,
        le=180.0,
    )
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

    @model_validator(mode="after")
    def coordinate_pairs_match_status_and_quality(self) -> Self:
        has_coordinate = self.latitude is not None
        if has_coordinate != (self.longitude is not None):
            raise ValueError("site coordinate pair must be both present or both absent")
        has_anchor = self.routing_anchor_latitude is not None
        if has_anchor != (self.routing_anchor_longitude is not None):
            raise ValueError("routing anchor pair must be both present or both absent")
        if has_coordinate != has_anchor:
            raise ValueError("site coordinate and routing anchor presence must match")
        if has_coordinate == (self.coordinate_quality == "MISSING"):
            raise ValueError("coordinateQuality must match coordinate presence")
        if self.status is InstitutionStatus.ACTIVE and not has_coordinate:
            raise ValueError("ACTIVE site requires coordinates and a routing anchor")
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


def _require_sorted_positive_count_map(
    values: dict[str, int],
    label: str,
) -> dict[str, int]:
    if (
        type(values) is not dict
        or list(values) != sorted(values)
        or any(type(name) is not str or not name.strip() for name in values)
        or any(type(count) is not int or count <= 0 for count in values.values())
    ):
        raise ValueError(f"{label} must be sorted positive counts")
    return values


class SchoolCountCategoryResult(_StrictManifestContractModel):
    expected_count: int = Field(ge=0)
    actual_count: int = Field(ge=0)
    delta_count: int
    status: Literal["MATCHED", "REVIEWED_VARIANCE"]

    @model_validator(mode="after")
    def delta_and_status_match(self) -> Self:
        if self.actual_count - self.expected_count != self.delta_count:
            raise ValueError("school count delta is inconsistent")
        expected_status = (
            "MATCHED" if self.delta_count == 0 else "REVIEWED_VARIANCE"
        )
        if self.status != expected_status:
            raise ValueError("school count status is inconsistent")
        return self


class SchoolCountSourceSummary(_StrictManifestContractModel):
    fetched_count: int = Field(ge=1)
    normalized_count: int = Field(ge=1)
    role_counts: dict[str, int]

    @field_validator("role_counts")
    @classmethod
    def roles_are_canonical(cls, values: dict[str, int]) -> dict[str, int]:
        return _require_sorted_positive_count_map(values, "roleCounts")

    @model_validator(mode="after")
    def totals_are_consistent(self) -> Self:
        if sum(self.role_counts.values()) != self.fetched_count:
            raise ValueError("source role counts must sum to fetchedCount")
        if self.normalized_count != self.fetched_count - self.role_counts.get(
            "NONSELECTABLE", 0
        ):
            raise ValueError("normalizedCount must exclude only NONSELECTABLE")
        return self


class SchoolCountReconciliation(_StrictManifestContractModel):
    profile_status: Literal["TEMPORARY_PRELIMINARY_VARIANCE"]
    profile_sha256: str
    benchmark_sha256: str
    sources: dict[str, SchoolCountSourceSummary]
    categories: dict[str, SchoolCountCategoryResult]
    passed: Literal[True]

    @field_validator("profile_sha256", "benchmark_sha256")
    @classmethod
    def hashes_are_lowercase_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @model_validator(mode="after")
    def matches_reviewed_contract(self) -> Self:
        if (
            self.profile_sha256 != _POPULATION_PROFILE_SHA256
            or self.benchmark_sha256 != _SCHOOL_COUNT_BENCHMARK_SHA256
            or list(self.sources) != sorted(_SOURCE_POPULATION_ROLE_COUNTS)
            or list(self.categories) != sorted(_SCHOOL_COUNT_CATEGORY_RESULTS)
        ):
            raise ValueError("school count reconciliation is not reviewed")
        for source, expected_roles in _SOURCE_POPULATION_ROLE_COUNTS.items():
            summary = self.sources.get(source)
            expected_fetched = sum(expected_roles.values())
            if (
                summary is None
                or summary.role_counts != expected_roles
                or summary.fetched_count != expected_fetched
                or summary.normalized_count
                != expected_fetched - expected_roles.get("NONSELECTABLE", 0)
            ):
                raise ValueError("school count source summary is not reviewed")
        for category, expected in _SCHOOL_COUNT_CATEGORY_RESULTS.items():
            result = self.categories.get(category)
            if result is None or (
                result.expected_count,
                result.actual_count,
                result.delta_count,
            ) != expected:
                raise ValueError("school count category result is not reviewed")
        return self


class SourceSnapshotInfo(_StrictManifestContractModel):
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str | None
    source_observation_date_counts: dict[str, int]
    normalized_observation_date_counts: dict[str, int]
    preserved_observation_date_counts: dict[str, int]
    raw_sha256: str
    source_normalized_sha256: str
    normalized_sha256: str
    request_region_code: str
    request_timing: str | None
    page_count: int = Field(ge=0)
    fetched_row_count: int = Field(ge=0)
    normalized_row_count: int = Field(ge=0)
    preserved_row_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    unclassified_school_kind_counts: dict[str, int]
    unclassified_school_policy_sha256: str | None
    source_category_counts: dict[str, int]
    source_population_role_counts: dict[str, int]
    source_population_profile_sha256: str | None

    @field_validator(
        "source",
        "endpoint",
        "license_name",
        "attribution",
        "fetched_at",
        "request_region_code",
    )
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator(
        "raw_sha256",
        "source_normalized_sha256",
        "normalized_sha256",
    )
    @classmethod
    def raw_hash_is_lowercase_sha256(cls, value: str) -> str:
        return _require_sha256(value)

    @field_validator("unclassified_school_kind_counts")
    @classmethod
    def unclassified_kind_counts_are_canonical(
        cls,
        values: dict[str, int],
    ) -> dict[str, int]:
        if (
            type(values) is not dict
            or list(values) != sorted(values)
            or any(type(name) is not str or not name.strip() for name in values)
            or any(type(count) is not int or count <= 0 for count in values.values())
        ):
            raise ValueError("unclassifiedSchoolKindCounts must be sorted positive counts")
        return values

    @field_validator("unclassified_school_policy_sha256")
    @classmethod
    def unclassified_policy_hash_is_lowercase_sha256(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("source_category_counts", "source_population_role_counts")
    @classmethod
    def population_counts_are_canonical(
        cls,
        values: dict[str, int],
    ) -> dict[str, int]:
        if values == {}:
            return values
        return _require_sorted_positive_count_map(values, "source population counts")

    @field_validator("source_population_profile_sha256")
    @classmethod
    def population_profile_hash_is_lowercase_sha256(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _require_sha256(value)

    @field_validator("source_as_of")
    @classmethod
    def source_date_is_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _parse_iso_date(value)
        return value

    @field_validator(
        "source_observation_date_counts",
        "normalized_observation_date_counts",
        "preserved_observation_date_counts",
    )
    @classmethod
    def observation_date_counts_are_canonical(
        cls,
        values: dict[str, int],
    ) -> dict[str, int]:
        entries = list(values.items())
        if list(values) != sorted(values):
            raise ValueError("observation date count keys must be sorted")
        for source_date, count in entries:
            _parse_iso_date(source_date)
            if type(count) is not int or count <= 0:
                raise ValueError("observation date counts must be positive integers")
        return values

    @field_validator("fetched_at")
    @classmethod
    def fetched_timestamp_is_timezone_aware(cls, value: str) -> str:
        _parse_rfc3339_timestamp(value)
        return value

    @model_validator(mode="after")
    def source_date_is_not_after_fetch(self) -> Self:
        raw_dates = list(self.source_observation_date_counts)
        canonical_source_as_of = raw_dates[0] if len(raw_dates) == 1 else None
        if self.source_as_of != canonical_source_as_of:
            raise ValueError(
                "sourceAsOf must equal the sole sourceObservationDateCounts date "
                "and must be null for mixed dates"
            )
        fetched_date = _parse_rfc3339_timestamp(self.fetched_at).date()
        if self.source_as_of is not None and _parse_iso_date(
            self.source_as_of
        ) > fetched_date:
            raise ValueError("sourceAsOf must not be later than fetchedAt date")
        observation_dates = {
            *self.source_observation_date_counts,
            *self.normalized_observation_date_counts,
            *self.preserved_observation_date_counts,
        }
        if any(_parse_iso_date(value) > fetched_date for value in observation_dates):
            raise ValueError(
                "observation dates must not be later than fetchedAt date"
            )
        if self.page_count <= 0 or self.fetched_row_count <= 0:
            raise ValueError("source page/fetched counts must be positive")
        if self.normalized_row_count > self.fetched_row_count:
            raise ValueError("normalizedRowCount must not exceed fetchedRowCount")
        if self.normalized_row_count + self.preserved_row_count != self.row_count:
            raise ValueError(
                "normalizedRowCount + preservedRowCount must equal rowCount"
            )
        if sum(self.source_observation_date_counts.values()) != self.fetched_row_count:
            raise ValueError(
                "sourceObservationDateCounts must sum to fetchedRowCount"
            )
        if (
            sum(self.normalized_observation_date_counts.values())
            != self.normalized_row_count
        ):
            raise ValueError(
                "normalizedObservationDateCounts must sum to normalizedRowCount"
            )
        if (
            sum(self.preserved_observation_date_counts.values())
            != self.preserved_row_count
        ):
            raise ValueError(
                "preservedObservationDateCounts must sum to preservedRowCount"
            )
        expected_categories = _SOURCE_CATEGORY_COUNTS.get(self.source)
        expected_roles = _SOURCE_POPULATION_ROLE_COUNTS.get(self.source)
        if expected_categories is None or expected_roles is None:
            if (
                self.source_category_counts
                or self.source_population_role_counts
                or self.source_population_profile_sha256 is not None
            ):
                raise ValueError("source population fields are reserved for NEIS/KGI")
        elif (
            self.source_category_counts != expected_categories
            or self.source_population_role_counts != expected_roles
            or self.source_population_profile_sha256 != _POPULATION_PROFILE_SHA256
            or sum(self.source_category_counts.values()) != self.fetched_row_count
            or sum(self.source_population_role_counts.values())
            != self.fetched_row_count
            or self.fetched_row_count
            - self.source_population_role_counts.get("NONSELECTABLE", 0)
            != self.normalized_row_count
        ):
            raise ValueError("source population provenance is not reviewed")
        return self

    @field_validator("request_timing")
    @classmethod
    def request_timing_is_nonblank_when_present(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _require_nonblank(value)


class EnrichmentSnapshotInfo(_StrictSnapshotModel):
    source: str
    endpoint: str
    license_name: str
    attribution: str
    fetched_at: str
    source_as_of: str
    raw_sha256: str
    source_normalized_sha256: str
    normalized_sha256: str
    request_region_code: str
    request_timing: str | None
    page_count: int = Field(ge=0)
    fetched_row_count: int = Field(ge=0)
    matched_row_count: int = Field(ge=0)
    preserved_matched_row_count: int = Field(ge=0)
    row_count: int = Field(ge=0)

    @field_validator(
        "source",
        "endpoint",
        "license_name",
        "attribution",
        "fetched_at",
        "source_as_of",
        "request_region_code",
    )
    @classmethod
    def required_strings_are_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)

    @field_validator(
        "raw_sha256",
        "source_normalized_sha256",
        "normalized_sha256",
    )
    @classmethod
    def hashes_are_lowercase_sha256(cls, value: str) -> str:
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

    @field_validator("request_timing")
    @classmethod
    def request_timing_is_nonblank_when_present(
        cls,
        value: str | None,
    ) -> str | None:
        return None if value is None else _require_nonblank(value)

    @model_validator(mode="after")
    def enrichment_counts_and_dates_are_consistent(self) -> Self:
        if self.matched_row_count > self.fetched_row_count:
            raise ValueError("matchedRowCount must not exceed fetchedRowCount")
        if self.matched_row_count + self.preserved_matched_row_count != self.row_count:
            raise ValueError(
                "matchedRowCount + preservedMatchedRowCount must equal rowCount"
            )
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


class PossibleInstitutionMatch(_StrictSnapshotModel):
    institution_ids: tuple[str, str]
    reason: str

    @field_validator("institution_ids")
    @classmethod
    def institution_pair_is_safe_sorted_and_distinct(
        cls,
        values: tuple[str, str],
    ) -> tuple[str, str]:
        for value in values:
            _require_namespaced_id(value, "institution")
        if values[0] >= values[1]:
            raise ValueError("institutionIds must be distinct and sorted")
        return values

    @field_validator("reason")
    @classmethod
    def reason_is_nonblank(cls, value: str) -> str:
        return _require_nonblank(value)


class SnapshotManifest(_StrictManifestContractModel):
    schema_version: int
    snapshot_id: str
    created_at: str
    snapshot_as_of: str
    approved: bool
    approved_at: str | None
    approved_by_role: str | None
    sources: tuple[SourceSnapshotInfo, ...]
    enrichments: tuple[EnrichmentSnapshotInfo, ...]
    institutions_sha256: str
    sites_sha256: str
    institution_count: int = Field(ge=0)
    site_count: int = Field(ge=0)
    quarantined_count: int = Field(ge=0)
    possible_match_count: int = Field(ge=0)
    possible_matches: tuple[PossibleInstitutionMatch, ...]
    counts_by_type: dict[str, int]
    counts_by_foundation: dict[str, int]
    counts_by_status: dict[str, int]
    coordinate_quality_counts: dict[str, int]
    school_count_reconciliation: SchoolCountReconciliation | None
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
        source_names = {source.source for source in self.sources}
        test_fixture_exception = (
            source_names == {"TEST_NEIS"}
            and self.approved_by_role == "TEST_FIXTURE_REVIEWER"
            and self.school_count_reconciliation is None
            and all(
                not source.source_category_counts
                and not source.source_population_role_counts
                and source.source_population_profile_sha256 is None
                for source in self.sources
            )
        )
        if self.school_count_reconciliation is None and not test_fixture_exception:
            raise ValueError("schoolCountReconciliation is required")
        if source_names & set(_SOURCE_POPULATION_ROLE_COUNTS):
            if self.school_count_reconciliation is None:
                raise ValueError("production school sources require reconciliation")
            manifest_sources = {source.source: source for source in self.sources}
            for source_name, summary in self.school_count_reconciliation.sources.items():
                source = manifest_sources.get(source_name)
                if (
                    source is None
                    or source.fetched_row_count != summary.fetched_count
                    or source.normalized_row_count != summary.normalized_count
                    or source.source_population_role_counts != summary.role_counts
                    or source.source_population_profile_sha256
                    != self.school_count_reconciliation.profile_sha256
                ):
                    raise ValueError(
                        "schoolCountReconciliation does not match source provenance"
                    )
        created_at = _parse_rfc3339_timestamp(self.created_at)
        approved_at = _parse_rfc3339_timestamp(self.approved_at)
        if created_at > approved_at:
            raise ValueError("createdAt must not be later than approvedAt")
        snapshot_as_of = _parse_iso_date(self.snapshot_as_of)
        if snapshot_as_of > created_at.date():
            raise ValueError("snapshotAsOf must not be later than createdAt date")
        for source in self.sources:
            if _parse_rfc3339_timestamp(source.fetched_at) > created_at:
                raise ValueError(
                    f"source {source.source} fetchedAt must not be later than "
                    "manifest createdAt"
                )
            if (
                source.source_as_of is not None
                and _parse_iso_date(source.source_as_of) > snapshot_as_of
            ):
                raise ValueError(
                    f"source {source.source} sourceAsOf must not be later than "
                    "manifest snapshotAsOf"
                )
            source_observation_dates = {
                *source.source_observation_date_counts,
                *source.normalized_observation_date_counts,
                *source.preserved_observation_date_counts,
            }
            if any(
                _parse_iso_date(value) > snapshot_as_of
                for value in source_observation_dates
            ):
                raise ValueError(
                    f"source {source.source} observation date must not be later "
                    "than manifest snapshotAsOf"
                )
        for enrichment in self.enrichments:
            if _parse_rfc3339_timestamp(enrichment.fetched_at) > created_at:
                raise ValueError(
                    f"enrichment {enrichment.source} fetchedAt must not be later "
                    "than manifest createdAt"
                )
            if _parse_iso_date(enrichment.source_as_of) > snapshot_as_of:
                raise ValueError(
                    f"enrichment {enrichment.source} sourceAsOf must not be later "
                    "than manifest snapshotAsOf"
                )
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
