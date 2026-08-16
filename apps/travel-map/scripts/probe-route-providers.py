import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

import httpx
from app.providers.http import ProviderRequestError
from app.providers.kakao_local import BoundingBox, KakaoLocalClient, PlaceCandidate
from app.providers.kakao_map import KakaoTransitProvider, KakaoWalkProvider
from app.providers.kakao_mobility import KakaoCarProvider
from app.providers.opinet import OpinetClient
from app.providers.seoul_transit import SeoulTransitProvider
from app.routing.models import (
    CarAssumptions,
    Coordinate,
    FuelType,
    ProviderResult,
    RouteQuery,
    TravelMode,
)
from app.settings import Settings

_CREDENTIALS = (
    ("KAKAO_REST_API_KEY", "kakao_rest_api_key"),
    ("OPINET_CERT_KEY", "opinet_cert_key"),
    ("SEOUL_TRANSIT_SERVICE_KEY", "seoul_transit_service_key"),
)
_SEOUL_BOUNDS = BoundingBox(126.70, 37.40, 127.30, 37.75)
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


async def _probe(
    args: argparse.Namespace,
    *,
    settings: Settings,
    http: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    if type(settings) is not Settings:
        raise TypeError("settings must be an exact Settings")
    if http is not None and type(http) is not httpx.AsyncClient:
        raise TypeError("http must be an exact AsyncClient or None")
    departure = datetime.now(UTC)
    local = KakaoLocalClient(http=http, rest_key=settings.kakao_rest_api_key)
    opinet = OpinetClient(
        http=http,
        cert_key=settings.opinet_cert_key,
        timeout_seconds=settings.provider_timeout_seconds,
    )
    providers = (
        SeoulTransitProvider(
            http=http,
            service_key=settings.seoul_transit_service_key,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
        KakaoTransitProvider(
            http=http,
            rest_key=settings.kakao_rest_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
        KakaoWalkProvider(
            http=http,
            rest_key=settings.kakao_rest_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
        KakaoCarProvider(
            http=http,
            rest_key=settings.kakao_rest_api_key,
            opinet=opinet,
            timeout_seconds=settings.provider_timeout_seconds,
        ),
    )
    operations: list[dict[str, object]] = []
    try:
        places = await local.search("서울특별시청", bounds=_SEOUL_BOUNDS)
        operations.append(
            _place_observation(
                "KAKAO_LOCAL_SEARCH",
                places,
                http_status=local.last_status_code,
                schema_fingerprint=local.last_schema_fingerprint,
                warnings=local.last_warnings,
            )
        )
        reverse = await local.reverse_geocode(args.origin)
        operations.append(
            _place_observation(
                "KAKAO_LOCAL_REVERSE",
                (() if reverse is None else (reverse,)),
                http_status=local.last_status_code,
                schema_fingerprint=local.last_schema_fingerprint,
                warnings=local.last_warnings,
            )
        )

        for provider in providers:
            mode = next(iter(provider.supported_modes))
            query = RouteQuery(
                origin=args.origin,
                destination=args.destination,
                depart_at=departure,
                mode=mode,
                car_assumptions=(
                    CarAssumptions(FuelType.GASOLINE, 10.0, 0)
                    if mode is TravelMode.CAR
                    else None
                ),
            )
            try:
                result = await provider.get_routes(query)
                operations.append(
                    _route_observation(
                        result,
                        http_status=provider.last_status_code,
                        schema_fingerprint=provider.last_schema_fingerprint,
                    )
                )
            except Exception:  # noqa: BLE001
                operations.append(
                    {
                        "operation": provider.name,
                        "status": "FAILED_CLOSED",
                        "httpStatus": provider.last_status_code,
                        "routeCount": 0,
                        "schemaFingerprint": provider.last_schema_fingerprint,
                        "warnings": ["UNEXPECTED_PROVIDER_FAILURE"],
                    }
                )

        try:
            fuel = await opinet.average_price(FuelType.GASOLINE)
            operations.append(
                {
                    "operation": "OPINET",
                    "status": "NORMALIZED",
                    "httpStatus": opinet.last_status_code,
                    "resultCount": 1,
                    "hasPrice": fuel.krw_per_liter > 0.0,
                    "hasTradeDate": fuel.trade_date is not None,
                    "schemaFingerprint": opinet.last_schema_fingerprint,
                }
            )
        except ProviderRequestError as exc:
            operations.append(
                {
                    "operation": "OPINET",
                    "status": "FAILED_CLOSED",
                    "httpStatus": opinet.last_status_code,
                    "resultCount": 0,
                    "schemaFingerprint": opinet.last_schema_fingerprint,
                    "warnings": [exc.code],
                }
            )
    finally:
        await _close_all((local, *providers, opinet))
    return {
        "status": "PROBED",
        "generatedAt": departure.isoformat(),
        "operationCount": len(operations),
        "operations": operations,
    }


def _place_observation(
    operation: str,
    places: tuple[PlaceCandidate, ...],
    *,
    http_status: int | None,
    schema_fingerprint: str | None,
    warnings: tuple[str, ...],
) -> dict[str, object]:
    return {
        "operation": operation,
        "status": _operation_status(http_status, len(places), warnings),
        "httpStatus": http_status,
        "placeCount": len(places),
        "hasStableIds": bool(places) and all(bool(place.place_id) for place in places),
        "hasNames": bool(places) and all(bool(place.name) for place in places),
        "hasAddresses": bool(places)
        and all(bool(place.road_address or place.lot_address) for place in places),
        "coordinatesValid": bool(places)
        and all(
            -90.0 <= place.latitude <= 90.0 and -180.0 <= place.longitude <= 180.0
            for place in places
        ),
        "schemaFingerprint": schema_fingerprint,
        "warnings": list(warnings),
    }


def _route_observation(
    result: ProviderResult,
    *,
    http_status: int | None,
    schema_fingerprint: str | None,
) -> dict[str, object]:
    warning_codes = [warning.code for warning in result.warnings]
    return {
        "operation": result.provider,
        "status": _operation_status(
            http_status,
            len(result.routes),
            tuple(warning_codes),
        ),
        "httpStatus": http_status,
        "routeCount": len(result.routes),
        "hasDuration": bool(result.routes)
        and all(route.duration_seconds >= 0 for route in result.routes),
        "hasDistance": bool(result.routes)
        and all(route.distance_meters >= 0 for route in result.routes),
        "hasCostCapability": bool(result.routes)
        and any(route.mobility_cost_krw is not None for route in result.routes),
        "hasGeometry": bool(result.routes)
        and all(len(route.geometry) >= 2 for route in result.routes),
        "schemaFingerprint": schema_fingerprint,
        "warnings": warning_codes,
    }


def _operation_status(
    http_status: int | None,
    result_count: int,
    warnings: tuple[str, ...],
) -> str:
    nonfailure_warnings = {
        "NO_RESULTS",
        "GEOMETRY_MISSING",
        "FARE_MISSING",
        "DUPLICATE_PLACE_ID",
        "OUT_OF_BOUNDS_RESULT",
    }
    if any(warning not in nonfailure_warnings for warning in warnings):
        return "FAILED_CLOSED"
    if http_status is not None and 200 <= http_status < 300:
        return "NORMALIZED" if result_count else "NO_RESULTS"
    return "FAILED_CLOSED" if warnings or http_status is not None else "NOT_CALLED"


async def _close_all(resources: tuple[object, ...]) -> bool:
    succeeded = True
    cancelled = False
    for resource in resources:
        try:
            await resource.aclose()  # type: ignore[attr-defined]
        except asyncio.CancelledError:
            cancelled = True
            succeeded = False
        except Exception:  # noqa: BLE001
            succeeded = False
    if cancelled:
        raise asyncio.CancelledError from None
    return succeeded


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


def _missing_credentials(settings: Settings) -> list[str]:
    return sorted(
        environment_name
        for environment_name, field_name in _CREDENTIALS
        if getattr(settings, field_name) is None
    )


def main() -> int:
    args = parse_args()
    if os.environ.get("TRAVEL_MAP_LIVE_SMOKE") != "1":
        report: dict[str, object] = {"status": "SKIPPED_NOT_OPTED_IN"}
    else:
        settings = Settings()
        missing = _missing_credentials(settings)
        if missing:
            report = {
                "status": "SKIPPED_MISSING_CREDENTIALS",
                "missingCredentials": missing,
            }
        else:
            report = asyncio.run(_probe(args, settings=settings))
    _write_atomic(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
