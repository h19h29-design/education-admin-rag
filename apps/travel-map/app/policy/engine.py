from app.policy.models import (
    AllowanceResult,
    AllowanceStatus,
    Classification,
    PolicyInput,
    PolicyProfile,
    PolicyResult,
    VehicleUse,
)
from app.policy.rules import RuleRepository, RuleSet


class PolicyEngine:
    def __init__(self, rule_repository: RuleRepository) -> None:
        self._rule_repository = rule_repository

    def calculate(self, policy_input: PolicyInput) -> PolicyResult:
        self._validate_input(policy_input)
        rules = self._rule_repository.for_date(policy_input.starts_at.date())
        classification = self._classify(policy_input, rules)
        duration_minutes = int(
            (policy_input.returns_at - policy_input.starts_at).total_seconds() // 60
        )
        calculated_status = (
            AllowanceStatus.REFERENCE_ESTIMATE
            if policy_input.policy_profile
            is PolicyProfile.INTERNAL_RULE_ADOPTION_CONFIRMED_BY_USER
            else AllowanceStatus.ESTIMATED
        )
        profile_source_refs = self._profile_source_refs(policy_input, rules)

        if classification is Classification.NON_LOCAL_EXPECTED:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
                warnings=("NON_LOCAL_ALLOWANCE_OUT_OF_SCOPE",),
            )
        elif policy_input.policy_profile is PolicyProfile.NONPUBLIC_OR_UNKNOWN:
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
            )
        elif policy_input.vehicle_use is VehicleUse.ASSIGNED_OFFICIAL:
            allowance = AllowanceResult(status=calculated_status, amount_krw=0)
        elif (
            policy_input.round_trip_distance_m <= rules.actual_expense_inclusive_meters
        ):
            allowance = AllowanceResult(
                status=AllowanceStatus.REVIEW_REQUIRED,
                amount_krw=None,
            )
        else:
            base = (
                rules.four_hours_or_more_krw
                if duration_minutes >= rules.four_hours_minutes
                else rules.under_four_hours_krw
            )
            if policy_input.vehicle_use is VehicleUse.OFFICIAL_OR_RENTED:
                base = max(0, base - rules.official_vehicle_deduction_krw)
            if (
                policy_input.has_other_local_trips_today
                and duration_minutes >= rules.four_hours_minutes
            ):
                base = max(
                    0,
                    rules.four_hours_or_more_krw - policy_input.previous_allowance_krw,
                )
            elif policy_input.has_other_local_trips_today:
                return PolicyResult(
                    classification=classification,
                    allowance=AllowanceResult(
                        status=AllowanceStatus.REVIEW_REQUIRED,
                        amount_krw=None,
                        warnings=("RULE_INTERPRETATION_UNVERIFIED",),
                    ),
                    rule_set_id=rules.rule_set_id,
                    effective_from=rules.effective_from.isoformat(),
                    source_refs=profile_source_refs,
                )
            allowance = AllowanceResult(
                status=calculated_status,
                amount_krw=base,
            )

        return PolicyResult(
            classification=classification,
            allowance=allowance,
            rule_set_id=rules.rule_set_id,
            effective_from=rules.effective_from.isoformat(),
            source_refs=profile_source_refs,
        )

    @staticmethod
    def _validate_input(policy_input: PolicyInput) -> None:
        if (
            policy_input.starts_at.tzinfo is None
            or policy_input.starts_at.utcoffset() is None
            or policy_input.returns_at.tzinfo is None
            or policy_input.returns_at.utcoffset() is None
        ):
            raise ValueError("starts_at and returns_at must be timezone-aware")
        if policy_input.returns_at <= policy_input.starts_at:
            raise ValueError("returns_at must be after starts_at")

    @staticmethod
    def _profile_source_refs(
        policy_input: PolicyInput,
        rules: RuleSet,
    ) -> tuple[str, ...]:
        if (
            policy_input.policy_profile
            is not PolicyProfile.NATIONAL_PUBLIC_OFFICIAL_CONFIRMED
        ):
            return rules.source_refs
        return tuple(
            source_ref
            for source_ref in rules.source_refs
            if "ordinInfoP.do?ordinSeq=2099835" not in source_ref
        )

    @staticmethod
    def _classify(policy_input: PolicyInput, rules: RuleSet) -> Classification:
        if policy_input.destination_in_seoul:
            return Classification.LOCAL
        if policy_input.round_trip_distance_m < rules.local_round_trip_exclusive_meters:
            return Classification.LOCAL
        return Classification.NON_LOCAL_EXPECTED
