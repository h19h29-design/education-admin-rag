import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from app.policy.rules import RuleRepository, RuleSet

REGULATION_URL = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=287535"


# Production break caught: selecting a newer rule before its effective date.
def test_repository_selects_latest_rule_effective_on_requested_date() -> None:
    january = make_rule("january", date(2026, 1, 1), under_four_hours_krw=8_000)
    july = make_rule("july", date(2026, 7, 1), under_four_hours_krw=10_000)
    repository = RuleRepository((july, january))

    assert repository.for_date(date(2026, 6, 30)).rule_set_id == "january"
    assert repository.for_date(date(2026, 7, 1)).rule_set_id == "july"


# Production break caught: silently applying a future rule to an uncovered date.
def test_repository_rejects_date_before_first_effective_rule() -> None:
    repository = RuleRepository((make_rule("july", date(2026, 7, 1)),))

    with pytest.raises(LookupError, match="no rule set for 2026-06-30"):
        repository.for_date(date(2026, 6, 30))


# Production break caught: making rule selection depend on input order for duplicate dates.
def test_repository_rejects_overlapping_effective_dates() -> None:
    first = make_rule("first", date(2026, 7, 1))
    second = make_rule("second", date(2026, 7, 1))

    with pytest.raises(ValueError, match="duplicate effective date: 2026-07-01"):
        RuleRepository((first, second))


# Production break caught: allowing any rule money field to become negative.
@pytest.mark.parametrize(
    "field_name",
    [
        "under_four_hours_krw",
        "four_hours_or_more_krw",
        "official_vehicle_deduction_krw",
    ],
)
def test_repository_rejects_negative_money_amount(field_name: str) -> None:
    invalid = replace(make_rule("invalid", date(2026, 7, 1)), **{field_name: -1})

    with pytest.raises(ValueError, match=rf"{field_name} must be non-negative"):
        RuleRepository((invalid,))


# Production break caught: publishing a calculable rule with no legal source.
def test_repository_rejects_rule_without_source_reference() -> None:
    invalid = replace(make_rule("invalid", date(2026, 7, 1)), source_refs=())

    with pytest.raises(ValueError, match="source_refs must not be empty"):
        RuleRepository((invalid,))


# Production break caught: silently selecting a payload date that contradicts the index.
def test_repository_rejects_index_and_payload_effective_date_mismatch(
    tmp_path: Path,
) -> None:
    (tmp_path / "index.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "effectiveFrom": "2026-07-01",
                        "file": "rule.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "rule.json").write_text(
        json.dumps(
            {
                "ruleSetId": "mismatched",
                "effectiveFrom": "2026-08-01",
                "localRoundTripExclusiveMeters": 12_000,
                "actualExpenseInclusiveMeters": 2_000,
                "fourHoursMinutes": 240,
                "underFourHoursKrw": 10_000,
                "fourHoursOrMoreKrw": 20_000,
                "officialVehicleDeductionKrw": 10_000,
                "sourceRefs": [REGULATION_URL],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="index and payload effectiveFrom differ for rule.json",
    ):
        RuleRepository.from_directory(tmp_path)


# Production break caught: coercing booleans, fractions, or numeric strings in
# JSON into materially different policy thresholds or amounts.
@pytest.mark.parametrize(
    ("json_field", "rule_field", "invalid_value"),
    [
        ("localRoundTripExclusiveMeters", "local_round_trip_exclusive_meters", True),
        ("fourHoursMinutes", "four_hours_minutes", 240.5),
        ("underFourHoursKrw", "under_four_hours_krw", "10000"),
    ],
)
def test_rule_loader_rejects_coerced_numeric_values(
    tmp_path: Path,
    json_field: str,
    rule_field: str,
    invalid_value: object,
) -> None:
    payload = make_rule_payload()
    payload[json_field] = invalid_value
    write_rule_directory(tmp_path, payload)

    with pytest.raises(ValueError, match=rf"{rule_field} must be an integer"):
        RuleRepository.from_directory(tmp_path)


# Production break caught: accepting zero or negative distance/time thresholds
# that invert or disable policy boundaries.
@pytest.mark.parametrize(
    "field_name",
    [
        "local_round_trip_exclusive_meters",
        "actual_expense_inclusive_meters",
        "four_hours_minutes",
    ],
)
def test_repository_requires_positive_distance_and_time_thresholds(
    field_name: str,
) -> None:
    invalid = replace(make_rule("invalid", date(2026, 7, 1)), **{field_name: 0})

    with pytest.raises(ValueError, match=rf"{field_name} must be positive"):
        RuleRepository((invalid,))


# Production break caught: making the actual-expense range overlap or exceed the
# complete local-trip range.
def test_repository_requires_actual_expense_threshold_below_local_threshold() -> None:
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        actual_expense_inclusive_meters=12_000,
    )

    with pytest.raises(
        ValueError,
        match=(
            "actual_expense_inclusive_meters must be less than "
            "local_round_trip_exclusive_meters"
        ),
    ):
        RuleRepository((invalid,))


# Production break caught: paying less for a four-hour trip than an under-four-hour trip.
def test_repository_orders_duration_allowance_tiers() -> None:
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        under_four_hours_krw=20_001,
    )

    with pytest.raises(
        ValueError,
        match="under_four_hours_krw must not exceed four_hours_or_more_krw",
    ):
        RuleRepository((invalid,))


# Production break caught: configuring a vehicle deduction larger than a tier to
# which that deduction is applied.
def test_repository_limits_vehicle_deduction_to_each_applicable_tier() -> None:
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        official_vehicle_deduction_krw=10_001,
    )

    with pytest.raises(
        ValueError,
        match="official_vehicle_deduction_krw must not exceed under_four_hours_krw",
    ):
        RuleRepository((invalid,))


# Production break caught: publishing a rule whose stable identifier is blank.
@pytest.mark.parametrize("rule_set_id", ["", "   "])
def test_repository_rejects_blank_rule_set_id(rule_set_id: str) -> None:
    with pytest.raises(ValueError, match="rule_set_id must be a non-blank string"):
        RuleRepository((make_rule(rule_set_id, date(2026, 7, 1)),))


# Production break caught: making two effective rule versions share one identifier.
def test_repository_rejects_duplicate_rule_set_ids() -> None:
    first = make_rule("same-id", date(2026, 7, 1))
    second = make_rule("same-id", date(2026, 8, 1))

    with pytest.raises(ValueError, match="duplicate rule_set_id: same-id"):
        RuleRepository((first, second))


# Production break caught: treating blank, non-web, hostless, or non-string source
# references as legal authority.
@pytest.mark.parametrize(
    "source_ref",
    ["", "   ", "ftp://example.com/rule", "https:///missing-host", 1],
)
def test_repository_requires_nonblank_http_source_urls(source_ref: object) -> None:
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        source_refs=(source_ref,),
    )

    with pytest.raises(
        ValueError,
        match=r"source_refs must contain only non-blank HTTP\(S\) URLs",
    ):
        RuleRepository((invalid,))


# Production break caught: silently ignoring a misspelled or unsupported rule key.
def test_rule_loader_rejects_unknown_payload_fields(tmp_path: Path) -> None:
    payload = make_rule_payload()
    payload["underFourHourKrw"] = 10_000
    write_rule_directory(tmp_path, payload)

    with pytest.raises(
        ValueError,
        match="rule payload must contain exactly the supported keys",
    ):
        RuleRepository.from_directory(tmp_path)


# Production break caught: accepting datetime or string effective values during
# direct construction and failing later inside for_date comparisons.
@pytest.mark.parametrize(
    "invalid_effective_from",
    [datetime(2026, 7, 1, tzinfo=UTC), "2026-07-01"],
)
def test_repository_rejects_non_date_effective_from_immediately(
    invalid_effective_from: object,
) -> None:
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        effective_from=invalid_effective_from,
    )

    with pytest.raises(TypeError, match="effective_from must be a date"):
        RuleRepository((invalid,))


# Production break caught: retaining a mutable list of source references that can
# leak through a later PolicyResult after repository construction.
def test_repository_rejects_mutable_source_reference_collection() -> None:
    mutable_source_refs = [REGULATION_URL]
    invalid = replace(
        make_rule("invalid", date(2026, 7, 1)),
        source_refs=mutable_source_refs,
    )

    with pytest.raises(TypeError, match="source_refs must be a tuple"):
        RuleRepository((invalid,))


def make_rule(
    rule_set_id: str,
    effective_from: date,
    *,
    under_four_hours_krw: int = 10_000,
) -> RuleSet:
    return RuleSet(
        rule_set_id=rule_set_id,
        effective_from=effective_from,
        local_round_trip_exclusive_meters=12_000,
        actual_expense_inclusive_meters=2_000,
        four_hours_minutes=240,
        under_four_hours_krw=under_four_hours_krw,
        four_hours_or_more_krw=20_000,
        official_vehicle_deduction_krw=min(10_000, under_four_hours_krw),
        source_refs=(REGULATION_URL,),
    )


def make_rule_payload() -> dict[str, object]:
    return {
        "ruleSetId": "local-travel-2026-07-01",
        "effectiveFrom": "2026-07-01",
        "localRoundTripExclusiveMeters": 12_000,
        "actualExpenseInclusiveMeters": 2_000,
        "fourHoursMinutes": 240,
        "underFourHoursKrw": 10_000,
        "fourHoursOrMoreKrw": 20_000,
        "officialVehicleDeductionKrw": 10_000,
        "sourceRefs": [REGULATION_URL],
    }


def write_rule_directory(root: Path, payload: dict[str, object]) -> None:
    (root / "index.json").write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "effectiveFrom": "2026-07-01",
                        "file": "rule.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "rule.json").write_text(json.dumps(payload), encoding="utf-8")
