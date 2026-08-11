import argparse
import asyncio
import hashlib
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
                warnings=local.last_warnings,
            )
        )
        reverse = await local.reverse_geocode(args.origin)
        operations.append(
            _place_observation(
                "KAKAO_LOCAL_REVERSE",
                (() if reverse is None else (reverse,)),
                http_status=local.last_status_code,
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
                    )
                )
            except Exception:  # noqa: BLE001
                operations.append(
                    _finalize_observation(
                        {
                            "operation": provider.name,
                            "status": "FAILED_CLOSED",
                            "httpStatus": provider.last_status_code,
                            "routeCount": 0,
                            "routes": [],
                            "warnings": ["UNEXPECTED_PROVIDER_FAILURE"],
                        }
                    )
                )

        try:
            fuel = await opinet.average_price(FuelType.GASOLINE)
            operations.append(
                _finalize_observation(
                    {
                        "operation": "OPINET",
                        "status": "NORMALIZED",
                        "httpStatus": opinet.last_status_code,
                        "fuelType": fuel.fuel_type.value,
                        "krwPerLiter": fuel.krw_per_liter,
                        "tradeDate": fuel.trade_date.isoformat(),
                        "source": fuel.source,
                    }
                )
            )
        except ProviderRequestError as exc:
            operations.append(
                _finalize_observation(
                    {
                        "operation": "OPINET",
                        "status": "FAILED_CLOSED",
                        "httpStatus": opinet.last_status_code,
                        "warning": exc.code,
                    }
                )
            )
    finally:
        await local.aclose()
        for provider in providers:
            await provider.aclose()
        await opinet.aclose()
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
    warnings: tuple[str, ...],
) -> dict[str, object]:
    observed_places = [
        {
            "placeIdPresent": bool(place.place_id),
            "namePresent": bool(place.name),
            "hasRoadAddress": bool(place.road_address),
            "hasLotAddress": bool(place.lot_address),
            "coordinateValid": (
                -90.0 <= place.latitude <= 90.0 and -180.0 <= place.longitude <= 180.0
            ),
        }
        for place in places
    ]
    return _finalize_observation(
        {
            "operation": operation,
            "status": _operation_status(http_status, len(places), warnings),
            "httpStatus": http_status,
            "placeCount": len(places),
            "places": observed_places,
            "warnings": list(warnings),
        }
    )


def _route_observation(
    result: ProviderResult,
    *,
    http_status: int | None,
) -> dict[str, object]:
    routes = [
        {
            "mode": route.mode.value,
            "durationSeconds": route.duration_seconds,
            "distanceMeters": route.distance_meters,
            "costStatus": route.cost_status.value,
            "mobilityCostKrw": route.mobility_cost_krw,
            "geometryPointCount": len(route.geometry),
            "source": route.source,
            "sourceAsOf": route.source_as_of.isoformat(),
        }
        for route in result.routes
    ]
    warning_codes = [warning.code for warning in result.warnings]
    return _finalize_observation(
        {
            "operation": result.provider,
            "status": _operation_status(
                http_status,
                len(result.routes),
                tuple(warning_codes),
            ),
            "httpStatus": http_status,
            "routeCount": len(result.routes),
            "routes": routes,
            "warnings": warning_codes,
        }
    )


def _operation_status(
    http_status: int | None,
    result_count: int,
    warnings: tuple[str, ...],
) -> str:
    if http_status is not None and 200 <= http_status < 300:
        return "NORMALIZED" if result_count else "NO_RESULTS"
    return "FAILED_CLOSED" if warnings or http_status is not None else "NOT_CALLED"


def _finalize_observation(value: dict[str, object]) -> dict[str, object]:
    encoded_shape = json.dumps(
        _observed_shape(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    value["schemaFingerprint"] = hashlib.sha256(encoded_shape).hexdigest()
    return value


def _observed_shape(value: object) -> object:
    if type(value) is dict:
        return {
            key: _observed_shape(item)
            for key, item in sorted(value.items())
            if type(key) is str
        }
    if type(value) is list:
        unique_items = {
            json.dumps(
                _observed_shape(item),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in value
        }
        return {
            "type": "array",
            "observedItemShapes": sorted(unique_items),
        }
    if value is None:
        return "null"
    return type(value).__name__


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
