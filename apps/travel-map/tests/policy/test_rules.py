import json
from dataclasses import replace
from datetime import date
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


# Production break caught: allowing a rule file to produce a negative allowance.
def test_repository_rejects_negative_money_amount() -> None:
    invalid = replace(make_rule("invalid", date(2026, 7, 1)), under_four_hours_krw=-1)

    with pytest.raises(ValueError, match="under_four_hours_krw must be non-negative"):
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
        official_vehicle_deduction_krw=10_000,
        source_refs=(REGULATION_URL,),
    )
