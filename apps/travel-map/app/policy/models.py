from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class CoverageState(StrEnum):
    SEOUL = "SEOUL"
    BUFFER = "BUFFER"
    OUTSIDE = "OUTSIDE"


class PolicyProfile(StrEnum):
    SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED = "SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED"
    NATIONAL_PUBLIC_OFFICIAL_CONFIRMED = "NATIONAL_PUBLIC_OFFICIAL_CONFIRMED"
    INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER = (
        "INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER"
    )
    NONPUBLIC_OR_UNKNOWN = "NONPUBLIC_OR_UNKNOWN"


class VehicleUse(StrEnum):
    NONE = "NONE"
    PRIVATE = "PRIVATE"
    OFFICIAL_OR_RENTED = "OFFICIAL_OR_RENTED"
    ASSIGNED_OFFICIAL = "ASSIGNED_OFFICIAL"


class Classification(StrEnum):
    LOCAL = "LOCAL"
    NON_LOCAL_EXPECTED = "NON_LOCAL_EXPECTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class AllowanceStatus(StrEnum):
    ESTIMATED = "ESTIMATED"
    REFERENCE_ESTIMATE = "REFERENCE_ESTIMATE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass(frozen=True)
class PolicyInput:
    destination_in_seoul: bool
    round_trip_distance_m: int
    starts_at: datetime
    returns_at: datetime
    policy_profile: PolicyProfile
    vehicle_use: VehicleUse
    has_other_local_trips_today: bool
    previous_allowance_krw: int


@dataclass(frozen=True)
class AllowanceResult:
    status: AllowanceStatus
    amount_krw: int | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyResult:
    classification: Classification
    allowance: AllowanceResult
    rule_set_id: str
    effective_from: str
    source_refs: tuple[str, ...]
