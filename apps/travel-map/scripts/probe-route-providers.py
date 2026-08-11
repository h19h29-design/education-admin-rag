import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from app.providers.kakao_map import KakaoTransitProvider, KakaoWalkProvider
from app.providers.kakao_mobility import KakaoCarProvider
from app.providers.opinet import OpinetClient
from app.providers.seoul_transit import SeoulTransitProvider
from app.routing.models import Coordinate, ProviderResult, RouteQuery, TravelMode
from app.settings import Settings

_CREDENTIALS = (
    "KAKAO_REST_API_KEY",
    "OPINET_CERT_KEY",
    "SEOUL_TRANSIT_SERVICE_KEY",
)
_MAX_REPORT_BYTES = 20_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Opt-in normalized provider contract probe (never stores raw payloads)."
    )
    parser.add_argument("--origin", required=True, type=_coordinate)
    parser.add_argument("--destination", required=True, type=_coordinate)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/provider-contract-report.json"),
    )
    return parser.parse_args()


def _coordinate(value: str) -> Coordinate:
    try:
        parts = value.split(",")
        if len(parts) != 2:
            raise ValueError
        longitude, latitude = (float(part) for part in parts)
        if not all(isfinite(part) for part in (latitude, longitude)):
            raise ValueError
        if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
            raise ValueError
        return Coordinate(latitude=latitude, longitude=longitude)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "coordinate must be finite longitude,latitude"
        ) from None


async def _probe(args: argparse.Namespace) -> dict[str, object]:
    settings = Settings()
    departure = datetime.now(UTC)
    base = RouteQuery(
        origin=args.origin,
        destination=args.destination,
        depart_at=departure,
        mode=TravelMode.TRANSIT,
    )
    opinet = OpinetClient(
        cert_key=settings.opinet_cert_key,
        timeout_seconds=settings.provider_timeout_seconds,
    )
    providers = (
        SeoulTransitProvider.from_settings(settings),
        KakaoTransitProvider.from_settings(settings),
        KakaoWalkProvider.from_settings(settings),
        KakaoCarProvider(
            rest_key=settings.kakao_rest_api_key,
            opinet=opinet,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
    )
    reports: list[dict[str, object]] = []
    try:
        for provider in providers:
            mode = next(iter(provider.supported_modes))
            query = RouteQuery(
                origin=base.origin,
                destination=base.destination,
                depart_at=base.depart_at,
                mode=mode,
            )
            try:
                result = await provider.get_routes(query)
                reports.append(_normalized_result(result))
            except Exception:  # noqa: BLE001
                reports.append(
                    {
                        "provider": provider.name,
                        "status": "FAILED_CLOSED",
                        "schemaFingerprint": _fingerprint(("provider", "status")),
                    }
                )
    finally:
        for provider in providers:
            await provider.aclose()
        await opinet.aclose()
    return {
        "status": "PROBED",
        "generatedAt": departure.isoformat(),
        "providers": reports,
    }


def _normalized_result(result: ProviderResult) -> dict[str, object]:
    routes = [
        {
            "mode": route.mode.value,
            "durationSeconds": route.duration_seconds,
            "distanceMeters": route.distance_meters,
            "costStatus": route.cost_status.value,
            "hasFare": route.mobility_cost_krw is not None,
            "hasGeometry": len(route.geometry) >= 2,
        }
        for route in result.routes[:10]
    ]
    fields = (
        "provider",
        "status",
        "routes.mode",
        "routes.durationSeconds",
        "routes.distanceMeters",
        "routes.costStatus",
        "routes.hasFare",
        "routes.hasGeometry",
        "warnings",
    )
    return {
        "provider": result.provider,
        "status": "NORMALIZED" if result.routes else "FAILED_CLOSED",
        "routes": routes,
        "warnings": [warning.code for warning in result.warnings[:20]],
        "schemaFingerprint": _fingerprint(fields),
    }


def _fingerprint(fields: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(fields).encode()).hexdigest()


def _write_atomic(output: Path, report: dict[str, object]) -> None:
    encoded = (
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(encoded) >= _MAX_REPORT_BYTES:
        raise RuntimeError("normalized provider report exceeds its byte limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    missing = sorted(
        name for name in _CREDENTIALS if not os.environ.get(name, "").strip()
    )
    if os.environ.get("TRAVEL_MAP_LIVE_SMOKE") != "1":
        report: dict[str, object] = {"status": "SKIPPED_NOT_OPTED_IN"}
    elif missing:
        report = {
            "status": "SKIPPED_MISSING_CREDENTIALS",
            "missingCredentials": missing,
        }
    else:
        report = asyncio.run(_probe(args))
    _write_atomic(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
