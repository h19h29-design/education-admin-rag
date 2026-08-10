import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class RuleSet:
    rule_set_id: str
    effective_from: date
    local_round_trip_exclusive_meters: int
    actual_expense_inclusive_meters: int
    four_hours_minutes: int
    under_four_hours_krw: int
    four_hours_or_more_krw: int
    official_vehicle_deduction_krw: int
    source_refs: tuple[str, ...]


class RuleRepository:
    def __init__(self, rules: tuple[RuleSet, ...]) -> None:
        effective_dates: set[date] = set()
        for rule in rules:
            if rule.effective_from in effective_dates:
                raise ValueError(
                    f"duplicate effective date: {rule.effective_from.isoformat()}"
                )
            effective_dates.add(rule.effective_from)
            self._validate_rule(rule)
        self._rules = tuple(sorted(rules, key=lambda item: item.effective_from))

    @classmethod
    def from_directory(cls, directory: str | Path) -> "RuleRepository":
        root = Path(directory)
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        rules: list[RuleSet] = []
        for entry in index["rules"]:
            payload = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
            if payload["effectiveFrom"] != entry["effectiveFrom"]:
                raise ValueError(
                    f"index and payload effectiveFrom differ for {entry['file']}"
                )
            rules.append(
                RuleSet(
                    rule_set_id=str(payload["ruleSetId"]),
                    effective_from=date.fromisoformat(payload["effectiveFrom"]),
                    local_round_trip_exclusive_meters=int(
                        payload["localRoundTripExclusiveMeters"]
                    ),
                    actual_expense_inclusive_meters=int(
                        payload["actualExpenseInclusiveMeters"]
                    ),
                    four_hours_minutes=int(payload["fourHoursMinutes"]),
                    under_four_hours_krw=int(payload["underFourHoursKrw"]),
                    four_hours_or_more_krw=int(payload["fourHoursOrMoreKrw"]),
                    official_vehicle_deduction_krw=int(
                        payload["officialVehicleDeductionKrw"]
                    ),
                    source_refs=tuple(str(url) for url in payload["sourceRefs"]),
                )
            )
        return cls(tuple(rules))

    def for_date(self, on_date: date) -> RuleSet:
        eligible = [item for item in self._rules if item.effective_from <= on_date]
        if not eligible:
            raise LookupError(f"no rule set for {on_date.isoformat()}")
        return eligible[-1]

    @staticmethod
    def _validate_rule(rule: RuleSet) -> None:
        money_fields = (
            "under_four_hours_krw",
            "four_hours_or_more_krw",
            "official_vehicle_deduction_krw",
        )
        for field_name in money_fields:
            if getattr(rule, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if not rule.source_refs:
            raise ValueError("source_refs must not be empty")
