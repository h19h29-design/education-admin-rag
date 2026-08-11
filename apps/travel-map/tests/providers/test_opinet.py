from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from app.providers.http import ProviderRequestError
from app.providers.opinet import FuelPrice, OpinetClient, estimate_car_cost
from app.routing.models import CarAssumptions, FuelType, RouteCostBreakdown
from pydantic import SecretStr
from tests.providers.helpers import NOW, load_json


def test_fuel_price_and_car_cost_use_strict_exact_inputs() -> None:
    with pytest.raises(TypeError):
        FuelPrice(FuelType.GASOLINE, 1700, NOW.date())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FuelPrice(FuelType.GASOLINE, float("inf"), NOW.date())
    with pytest.raises(TypeError):
        estimate_car_cost(
            distance_meters=True,  # type: ignore[arg-type]
            fuel_price_krw_per_liter=1700.0,
            assumptions=CarAssumptions(FuelType.GASOLINE, 10.0, 2000),
            toll_krw=1000,
        )

    breakdown = estimate_car_cost(
        distance_meters=20_000,
        fuel_price_krw_per_liter=1_700.0,
        assumptions=CarAssumptions(
            fuel_type=FuelType.GASOLINE,
            efficiency_km_per_liter=10.0,
            parking_cost_krw=2_000,
        ),
        toll_krw=1_000,
    )

    assert breakdown == RouteCostBreakdown(
        fuel_krw=3_400,
        toll_krw=1_000,
        parking_krw=2_000,
    )
    assert breakdown.total_krw == 6_400


@pytest.mark.asyncio
async def test_opinet_requires_certkey_uses_official_codes_and_caches() -> None:
    secret = "opinet+a/b?secret"
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.params["out"] == "json"
        assert request.url.params["certkey"] == secret
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("opinet-average.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(
            http=http,
            cert_key=SecretStr(secret),
            now=lambda: NOW,
            cache_ttl_seconds=3600.0,
        )
        gasoline = await client.average_price(FuelType.GASOLINE)
        diesel = await client.average_price(FuelType.DIESEL)
        gasoline_cached = await client.average_price(FuelType.GASOLINE)

    assert gasoline.krw_per_liter == 1700.0
    assert gasoline.trade_date.isoformat() == "2026-08-10"
    assert diesel.krw_per_liter == 1600.0
    assert gasoline_cached is gasoline
    assert requests == 1
    assert secret not in repr(client)


# Break caught: the production-default fuel cache refreshing before the required
# one-day TTL has elapsed.
@pytest.mark.asyncio
async def test_opinet_default_cache_ttl_is_exactly_one_day() -> None:
    current = [NOW]
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("opinet-average.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(
            http=http,
            cert_key=SecretStr("test-key"),
            now=lambda: current[0],
        )
        await client.average_price(FuelType.GASOLINE)
        current[0] += timedelta(seconds=3_601)
        await client.average_price(FuelType.GASOLINE)
        current[0] += timedelta(seconds=82_799)
        await client.average_price(FuelType.GASOLINE)

    assert requests == 2


@pytest.mark.asyncio
async def test_opinet_expired_cache_does_not_fabricate_price_on_failure() -> None:
    current = [NOW]
    responses = [
        httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("opinet-average.json"),
        ),
        httpx.Response(
            503,
            headers={"Content-Type": "application/json"},
            json={"error": "unavailable"},
        ),
        httpx.Response(
            503,
            headers={"Content-Type": "application/json"},
            json={"error": "unavailable"},
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(
            http=http,
            cert_key=SecretStr("test-key"),
            now=lambda: current[0],
            cache_ttl_seconds=60.0,
        )
        await client.average_price(FuelType.LPG)
        current[0] += timedelta(seconds=61)
        with pytest.raises(ProviderRequestError) as raised:
            await client.average_price(FuelType.LPG)

    assert raised.value.code == "UPSTREAM_UNAVAILABLE"
    assert "test-key" not in repr(raised.value)


@pytest.mark.asyncio
async def test_opinet_rejects_wrong_schema_and_nonfinite_prices() -> None:
    malformed = load_json("opinet-average.json")
    malformed["RESULT"]["OIL"][0]["PRICE"] = "NaN"  # type: ignore[index]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=malformed,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(http=http, cert_key=SecretStr("test-key"))
        with pytest.raises(ProviderRequestError) as raised:
            await client.average_price(FuelType.GASOLINE)

    assert raised.value.code == "SCHEMA_MISMATCH"


@pytest.mark.asyncio
async def test_opinet_missing_key_fails_closed_before_network() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(http=http, cert_key=None)
        with pytest.raises(ProviderRequestError) as raised:
            await client.average_price(FuelType.GASOLINE)

    assert raised.value.code == "MISSING_CREDENTIAL"
    assert requests == 0


def test_fuel_price_rejects_naive_or_wrong_trade_date_type() -> None:
    with pytest.raises(TypeError):
        FuelPrice(FuelType.GASOLINE, 1700.0, datetime.now(ZoneInfo("Asia/Seoul")))  # type: ignore[arg-type]
