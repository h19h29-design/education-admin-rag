from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.policy.engine import PolicyEngine
from app.policy.models import PolicyInput, PolicyProfile, VehicleUse
from app.policy.rules import RuleRepository

SEOUL = ZoneInfo("Asia/Seoul")
REGULATION_URL = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535"
SEOUL_ORDINANCE_URL = "https://www.law.go.kr/LSW/ordinInfoP.do?ordinSeq=2099835"


# Production break caught: including exactly 12,000 m in the local outside-Seoul rule.
@pytest.mark.parametrize(
    ("round_trip_m", "expected_classification"),
    [(11_999, "LOCAL"), (12_000, "NON_LOCAL_EXPECTED")],
)
def test_outside_seoul_uses_strict_twelve_km_boundary(
    round_trip_m: int, expected_classification: str
) -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            destination_in_seoul=False,
            round_trip_distance_m=round_trip_m,
        )
    )

    assert result.classification.value == expected_classification


# Production break caught: using a route estimate or the wrong 239/240-minute boundary.
@pytest.mark.parametrize(("minutes", "amount"), [(239, 10_000), (240, 20_000)])
def test_duration_boundary_uses_entered_trip_time(minutes: int, amount: int) -> None:
    result = make_policy_engine().calculate(make_policy_input(minutes=minutes))

    assert result.allowance.amount_krw == amount


# Production break caught: automatically paying a nonpublic or unknown traveler.
def test_unknown_employment_profile_withholds_allowance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(policy_profile=PolicyProfile.NONPUBLIC_OR_UNKNOWN)
    )

    assert result.allowance.status.value == "REVIEW_REQUIRED"
    assert result.allowance.amount_krw is None


# Production break caught: applying the local flat allowance to a non-local result.
def test_non_local_result_does_not_apply_local_flat_allowance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(destination_in_seoul=False, round_trip_distance_m=12_000)
    )

    assert result.classification.value == "NON_LOCAL_EXPECTED"
    assert result.allowance.status.value == "REVIEW_REQUIRED"
    assert result.allowance.amount_krw is None
    assert result.allowance.warnings == ("NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE",)


# Production break caught: using network distance to mark a Seoul destination non-local.
def test_destination_in_seoul_is_local_even_when_round_trip_exceeds_twelve_km() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(destination_in_seoul=True, round_trip_distance_m=50_000)
    )

    assert result.classification.value == "LOCAL"
    assert result.allowance.amount_krw == 10_000


# Production break caught: treating exactly 2,000 m as eligible for a flat allowance.
@pytest.mark.parametrize(
    ("round_trip_distance_m", "status", "amount_krw"),
    [(2_000, "REVIEW_REQUIRED", None), (2_001, "ESTIMATED", 10_000)],
)
def test_actual_expense_branch_is_inclusive_at_two_km(
    round_trip_distance_m: int, status: str, amount_krw: int | None
) -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            destination_in_seoul=True,
            round_trip_distance_m=round_trip_distance_m,
        )
    )

    assert result.classification.value == "LOCAL"
    assert result.allowance.status.value == status
    assert result.allowance.amount_krw == amount_krw


# Production break caught: skipping the 10,000 won official/rented vehicle deduction.
@pytest.mark.parametrize(("minutes", "amount_krw"), [(239, 0), (240, 10_000)])
def test_official_or_rented_vehicle_deducts_ten_thousand_won(
    minutes: int, amount_krw: int
) -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            minutes=minutes,
            vehicle_use=VehicleUse.OFFICIAL_OR_RENTED,
        )
    )

    assert result.allowance.amount_krw == amount_krw


# Production break caught: deducting an allowance instead of setting assigned vehicle to zero.
def test_assigned_official_vehicle_receives_zero_even_within_two_km() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            round_trip_distance_m=2_000,
            vehicle_use=VehicleUse.ASSIGNED_OFFICIAL,
        )
    )

    assert result.allowance.status.value == "ESTIMATED"
    assert result.allowance.amount_krw == 0


# Production break caught: applying a vehicle deduction to private vehicle use.
def test_private_vehicle_keeps_the_flat_allowance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(vehicle_use=VehicleUse.PRIVATE)
    )

    assert result.allowance.amount_krw == 10_000


# Production break caught: exceeding the 20,000 won same-day four-hour allowance ceiling.
@pytest.mark.parametrize(
    ("previous_allowance_krw", "amount_krw"), [(5_000, 15_000), (25_000, 0)]
)
def test_same_day_four_hour_trip_uses_remaining_daily_ceiling(
    previous_allowance_krw: int, amount_krw: int
) -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            minutes=240,
            has_other_local_trips_today=True,
            previous_allowance_krw=previous_allowance_krw,
        )
    )

    assert result.allowance.amount_krw == amount_krw


# Production break caught: inventing a same-day under-four-hour payment interpretation.
def test_same_day_under_four_hour_trip_requires_review() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            minutes=239,
            has_other_local_trips_today=True,
            previous_allowance_krw=10_000,
        )
    )

    assert result.allowance.status.value == "REVIEW_REQUIRED"
    assert result.allowance.amount_krw is None
    assert result.allowance.warnings == ("RULE_INTERPRETATION_UNVERIFIED",)


# Production break caught: presenting a user-confirmed internal adoption as official estimate.
def test_internal_rule_adoption_is_labeled_reference_estimate() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            policy_profile=PolicyProfile.INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER
        )
    )

    assert result.allowance.status.value == "REFERENCE_ESTIMATE"
    assert result.allowance.amount_krw == 10_000


# Production break caught: citing the Seoul education ordinance to a national official.
def test_national_official_sources_exclude_seoul_education_ordinance() -> None:
    result = make_policy_engine().calculate(
        make_policy_input(
            policy_profile=PolicyProfile.NATIONAL_PUBLIC_OFFICIAL_CONFIRMED
        )
    )

    assert REGULATION_URL in result.source_refs
    assert SEOUL_ORDINANCE_URL not in result.source_refs


# Production break caught: dropping the ordinance for a confirmed Seoul education official.
def test_seoul_education_official_sources_include_ordinance() -> None:
    result = make_policy_engine().calculate(make_policy_input())

    assert REGULATION_URL in result.source_refs
    assert SEOUL_ORDINANCE_URL in result.source_refs


# Production break caught: accepting an input time without an explicit timezone.
@pytest.mark.parametrize(("naive_field",), [("starts_at",), ("returns_at",)])
def test_policy_input_rejects_naive_datetime(naive_field: str) -> None:
    policy_input = make_policy_input()
    replacement = policy_input.starts_at.replace(tzinfo=None)
    values = {
        "destination_in_seoul": policy_input.destination_in_seoul,
        "round_trip_distance_m": policy_input.round_trip_distance_m,
        "starts_at": policy_input.starts_at,
        "returns_at": policy_input.returns_at,
        "policy_profile": policy_input.policy_profile,
        "vehicle_use": policy_input.vehicle_use,
        "has_other_local_trips_today": policy_input.has_other_local_trips_today,
        "previous_allowance_krw": policy_input.previous_allowance_krw,
    }
    values[naive_field] = replacement

    with pytest.raises(
        ValueError, match="starts_at and returns_at must be timezone-aware"
    ):
        make_policy_engine().calculate(PolicyInput(**values))


# Production break caught: calculating zero-length or backward trips.
@pytest.mark.parametrize(("minutes",), [(0,), (-1,)])
def test_policy_input_rejects_nonpositive_duration(minutes: int) -> None:
    with pytest.raises(ValueError, match="returns_at must be after starts_at"):
        make_policy_engine().calculate(make_policy_input(minutes=minutes))


def make_policy_engine() -> PolicyEngine:
    return PolicyEngine(
        RuleRepository.from_directory("apps/travel-map/resources/rules")
    )


def make_policy_input(
    *,
    minutes: int = 239,
    policy_profile: PolicyProfile = PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED,
    destination_in_seoul: bool = True,
    round_trip_distance_m: int = 3_000,
    vehicle_use: VehicleUse = VehicleUse.NONE,
    has_other_local_trips_today: bool = False,
    previous_allowance_krw: int = 0,
) -> PolicyInput:
    starts_at = datetime(2026, 8, 10, 9, 0, tzinfo=SEOUL)
    return PolicyInput(
        destination_in_seoul=destination_in_seoul,
        round_trip_distance_m=round_trip_distance_m,
        starts_at=starts_at,
        returns_at=starts_at + timedelta(minutes=minutes),
        policy_profile=policy_profile,
        vehicle_use=vehicle_use,
        has_other_local_trips_today=has_other_local_trips_today,
        previous_allowance_krw=previous_allowance_krw,
    )
