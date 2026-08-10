import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

RULE_PAYLOAD_KEYS = {
    "ruleSetId",
    "effectiveFrom",
    "localRoundTripExclusiveMeters",
    "actualExpenseInclusiveMeters",
    "fourHoursMinutes",
    "underFourHoursKrw",
    "fourHoursOrMoreKrw",
    "officialVehicleDeductionKrw",
    "sourceRefs",
}


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
        rule_set_ids: set[str] = set()
        for rule in rules:
            self._validate_rule(rule)
            if rule.rule_set_id in rule_set_ids:
                raise ValueError(f"duplicate rule_set_id: {rule.rule_set_id}")
            if rule.effective_from in effective_dates:
                raise ValueError(
                    f"duplicate effective date: {rule.effective_from.isoformat()}"
                )
            rule_set_ids.add(rule.rule_set_id)
            effective_dates.add(rule.effective_from)
        self._rules = tuple(sorted(rules, key=lambda item: item.effective_from))

    @classmethod
    def from_directory(cls, directory: str | Path) -> "RuleRepository":
        root = Path(directory)
        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        rules: list[RuleSet] = []
        for entry in index["rules"]:
            payload = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != RULE_PAYLOAD_KEYS:
                raise ValueError("rule payload must contain exactly the supported keys")
            if payload["effectiveFrom"] != entry["effectiveFrom"]:
                raise ValueError(
                    f"index and payload effectiveFrom differ for {entry['file']}"
                )
            source_refs = payload["sourceRefs"]
            if not isinstance(source_refs, list):
                raise TypeError(
                    "source_refs must contain only non-blank HTTP(S) URLs"
                )
            rules.append(
                RuleSet(
                    rule_set_id=payload["ruleSetId"],
                    effective_from=date.fromisoformat(payload["effectiveFrom"]),
                    local_round_trip_exclusive_meters=payload[
                        "localRoundTripExclusiveMeters"
                    ],
                    actual_expense_inclusive_meters=payload[
                        "actualExpenseInclusiveMeters"
                    ],
                    four_hours_minutes=payload["fourHoursMinutes"],
                    under_four_hours_krw=payload["underFourHoursKrw"],
                    four_hours_or_more_krw=payload["fourHoursOrMoreKrw"],
                    official_vehicle_deduction_krw=payload[
                        "officialVehicleDeductionKrw"
                    ],
                    source_refs=tuple(source_refs),
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
        if type(rule.rule_set_id) is not str or not rule.rule_set_id.strip():
            raise ValueError("rule_set_id must be a non-blank string")
        if type(rule.effective_from) is not date:
            raise TypeError("effective_from must be a date")
        positive_fields = (
            "local_round_trip_exclusive_meters",
            "actual_expense_inclusive_meters",
            "four_hours_minutes",
        )
        money_fields = (
            "under_four_hours_krw",
            "four_hours_or_more_krw",
            "official_vehicle_deduction_krw",
        )
        for field_name in positive_fields + money_fields:
            if type(getattr(rule, field_name)) is not int:
                raise ValueError(f"{field_name} must be an integer")
        for field_name in positive_fields:
            if getattr(rule, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name in money_fields:
            if getattr(rule, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if (
            rule.actual_expense_inclusive_meters
            >= rule.local_round_trip_exclusive_meters
        ):
            raise ValueError(
                "actual_expense_inclusive_meters must be less than "
                "local_round_trip_exclusive_meters"
            )
        if rule.under_four_hours_krw > rule.four_hours_or_more_krw:
            raise ValueError(
                "under_four_hours_krw must not exceed four_hours_or_more_krw"
            )
        if rule.official_vehicle_deduction_krw > rule.under_four_hours_krw:
            raise ValueError(
                "official_vehicle_deduction_krw must not exceed "
                "under_four_hours_krw"
            )
        if type(rule.source_refs) is not tuple:
            raise TypeError("source_refs must be a tuple")
        if not rule.source_refs:
            raise ValueError("source_refs must not be empty")
        for source_ref in rule.source_refs:
            if type(source_ref) is not str or not source_ref.strip():
                raise ValueError(
                    "source_refs must contain only non-blank HTTP(S) URLs"
                )
            parsed = urlsplit(source_ref)
            if (
                source_ref != source_ref.strip()
                or any(character.isspace() for character in source_ref)
                or parsed.scheme not in {"http", "https"}
                or not parsed.hostname
            ):
                raise ValueError(
                    "source_refs must contain only non-blank HTTP(S) URLs"
                )
