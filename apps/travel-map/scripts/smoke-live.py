"""Opt-in, bounded release smoke checks with deliberately minimal telemetry."""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

from app.contracts import TripPreviewRequest, TripPreviewResponse
from app.dependencies import AppDependencies, build_production_dependencies
from app.institutions.models import InstitutionSearchItem
from app.institutions.snapshot import SnapshotIntegrityError, verify_snapshot
from app.policy.models import PolicyProfile, VehicleUse
from app.routing.models import FuelType
from app.services.trip_preview import TripPreviewService
from app.settings import Settings

_APP_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT_ROOT = _APP_ROOT / "resources/institution-snapshots"
_SEOUL_TZ = ZoneInfo("Asia/Seoul")
_CITY_HALL_LATITUDE = 37.5662952
_CITY_HALL_LONGITUDE = 126.9779451
_CITY_HALL: dict[str, str | float] = {
    "name": "서울특별시청",
    "address": "서울특별시 중구 세종대로 110",
    "latitude": _CITY_HALL_LATITUDE,
    "longitude": _CITY_HALL_LONGITUDE,
}
_OUT_OF_COVERAGE: dict[str, str | float] = {
    "name": "OUT_OF_COVERAGE_SENTINEL",
    "address": "OUT_OF_COVERAGE_SENTINEL",
    "latitude": 35.1796,
    "longitude": 129.0756,
}
_RUNTIME_CREDENTIAL_FIELDS = (
    "kakao_rest_api_key",
    "seoul_transit_service_key",
    "opinet_cert_key",
)


class SmokeExpectationError(RuntimeError):
    """A bounded live check completed but did not meet its release expectation."""


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    origin_site_id: str
    destination: dict[str, str | float]
    policy_profile: PolicyProfile
    expect_local: bool = False
    expect_no_allowance_amount: bool = False
    expect_out_of_coverage: bool = False


def _emit(report: dict[str, object]) -> None:
    """Write only a safe, one-line JSON release record to standard output."""

    sys.stdout.write(json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n")


def _has_runtime_credentials(settings: Settings) -> bool:
    return all(getattr(settings, field) is not None for field in _RUNTIME_CREDENTIAL_FIELDS)


def _case_report(
    case_id: str,
    response: TripPreviewResponse,
    *,
    latency_ms: int,
) -> dict[str, object]:
    """Return only the telemetry explicitly approved for the live smoke output."""

    route_ids = {route.id for route in response.routes}
    return {
        "caseId": case_id,
        "providerStatus": "SUCCESS" if response.routes else "NO_ROUTES",
        "routeCount": len(response.routes),
        "decision": response.classification,
        "latencyMs": latency_ms,
        "representativeRoutePresent": response.best.fastest_route_id in route_ids,
    }


def _nearest_active_site(dependencies: AppDependencies, foundation_type: str) -> str:
    candidates = dependencies.institutions.search(
        foundation_type=foundation_type,
        limit=50,
    )
    if not candidates:
        raise SmokeExpectationError("required foundation type is unavailable")

    def distance_key(item: InstitutionSearchItem) -> float:
        site = dependencies.institutions.require_site(item.site_id)
        if (
            site.routing_anchor_latitude is None
            or site.routing_anchor_longitude is None
        ):
            return float("inf")
        return (site.routing_anchor_latitude - _CITY_HALL_LATITUDE) ** 2 + (
            site.routing_anchor_longitude - _CITY_HALL_LONGITUDE
        ) ** 2

    selected = min(candidates, key=distance_key)
    if distance_key(selected) == float("inf"):
        raise SmokeExpectationError("required origin has no routing anchor")
    return selected.site_id


def _request_for(case: SmokeCase) -> TripPreviewRequest:
    starts_at = datetime.now(_SEOUL_TZ).replace(second=0, microsecond=0)
    returns_at = starts_at + timedelta(hours=4)
    return TripPreviewRequest.model_validate(
        {
            "originSiteId": case.origin_site_id,
            "destination": case.destination,
            "startsAt": starts_at.isoformat(),
            "returnsAt": returns_at.isoformat(),
            "policyProfile": case.policy_profile.value,
            "vehicleUse": VehicleUse.NONE.value,
            "carAssumptions": {
                "fuelType": FuelType.GASOLINE.value,
                "efficiencyKmPerLiter": 10.0,
                "parkingCostKrw": 0,
            },
            "hasOtherLocalTripsToday": False,
            "previousAllowanceKrw": 0,
        }
    )


async def _run_case(
    service: TripPreviewService,
    case: SmokeCase,
) -> tuple[TripPreviewResponse, dict[str, object]]:
    started = perf_counter()
    response = await service.preview(_request_for(case))
    report = _case_report(
        case.case_id,
        response,
        latency_ms=int((perf_counter() - started) * 1000),
    )
    _validate_case(case, response, report)
    return response, report


def _validate_case(
    case: SmokeCase,
    response: TripPreviewResponse,
    report: dict[str, object],
) -> None:
    if case.expect_out_of_coverage:
        if response.coverage.status != "OUT_OF_COVERAGE" or response.routes:
            raise SmokeExpectationError("out-of-coverage response was not fail-closed")
        return
    if not response.routes:
        raise SmokeExpectationError("a route response was required")
    if case.expect_local:
        mode_count = len({route.mode for route in response.routes})
        if response.classification != "LOCAL" or mode_count < 2:
            raise SmokeExpectationError("public local case did not meet route expectations")
        if report["representativeRoutePresent"] is not True:
            raise SmokeExpectationError("public local case has no representative route")
    if (
        case.expect_no_allowance_amount
        and response.allowance.amount_krw is not None
    ):
        raise SmokeExpectationError("nonpublic case exposed an allowance amount")


async def _run_cases(dependencies: AppDependencies) -> list[dict[str, object]]:
    service = TripPreviewService(dependencies)
    public_site_id = _nearest_active_site(dependencies, "PUBLIC")
    private_site_id = _nearest_active_site(dependencies, "PRIVATE")
    cases = (
        SmokeCase(
            case_id="PUBLIC_LOCAL",
            origin_site_id=public_site_id,
            destination=_CITY_HALL,
            policy_profile=PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED,
            expect_local=True,
        ),
        SmokeCase(
            case_id="NONPUBLIC",
            origin_site_id=private_site_id,
            destination=_CITY_HALL,
            policy_profile=PolicyProfile.NONPUBLIC_OR_UNKNOWN,
            expect_no_allowance_amount=True,
        ),
        SmokeCase(
            case_id="OUT_OF_COVERAGE",
            origin_site_id=public_site_id,
            destination=_OUT_OF_COVERAGE,
            policy_profile=PolicyProfile.SEOUL_EDU_PUBLIC_OFFICIAL_CONFIRMED,
            expect_out_of_coverage=True,
        ),
    )
    reports: list[dict[str, object]] = []
    for case in cases:
        _, report = await _run_case(service, case)
        reports.append(report)
    return reports


async def _execute(settings: Settings) -> list[dict[str, object]]:
    dependencies = build_production_dependencies(settings)
    try:
        return await _run_cases(dependencies)
    finally:
        await dependencies.aclose()


def main() -> int:
    if os.environ.get("TRAVEL_MAP_LIVE_SMOKE") != "1":
        _emit({"status": "REFUSED_NOT_OPTED_IN"})
        return 2
    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 - configuration details may be sensitive
        _emit({"status": "BLOCKED_INVALID_SETTINGS"})
        return 2
    if not _has_runtime_credentials(settings):
        _emit({"status": "BLOCKED_MISSING_CREDENTIALS"})
        return 2
    try:
        verify_snapshot(_SNAPSHOT_ROOT)
    except SnapshotIntegrityError:
        _emit({"status": "BLOCKED_MISSING_APPROVED_SNAPSHOT"})
        return 2
    try:
        reports = asyncio.run(_execute(settings))
    except SmokeExpectationError:
        _emit({"status": "FAILED_EXPECTATIONS"})
        return 1
    except Exception:  # noqa: BLE001 - provider details are never smoke output
        _emit({"status": "FAILED_EXECUTION"})
        return 1
    _emit({"status": "PASSED", "cases": reports})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
