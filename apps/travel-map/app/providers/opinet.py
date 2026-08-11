import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite

import httpx
from pydantic import SecretStr

from app.cache import FUEL_TTL_SECONDS
from app.providers.http import BoundedHttpClient, ProviderRequestError
from app.routing.models import (
    CarAssumptions,
    FuelType,
    RouteCostBreakdown,
)

_AVERAGE_URL = "https://www.opinet.co.kr/api/avgAllPrice.do"
_PRODUCT_CODES = {
    FuelType.GASOLINE: "B027",
    FuelType.DIESEL: "D047",
    FuelType.LPG: "K015",
}


@dataclass(frozen=True)
class FuelPrice:
    fuel_type: FuelType
    krw_per_liter: float
    trade_date: date
    source: str = "OPINET"

    def __post_init__(self) -> None:
        if type(self.fuel_type) is not FuelType:
            raise TypeError("fuel_type must be an exact FuelType")
        if type(self.krw_per_liter) is not float:
            raise TypeError("krw_per_liter must be an exact float")
        if not isfinite(self.krw_per_liter) or self.krw_per_liter <= 0.0:
            raise ValueError("krw_per_liter must be positive and finite")
        if type(self.trade_date) is not date:
            raise TypeError("trade_date must be an exact date")
        if type(self.source) is not str:
            raise TypeError("source must be an exact string")
        if self.source != "OPINET":
            raise ValueError("source must be OPINET")


class OpinetClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        cert_key: SecretStr | None = None,
        now: Callable[[], datetime] | None = None,
        cache_ttl_seconds: float = FUEL_TTL_SECONDS,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 300_000,
    ) -> None:
        if cert_key is not None and type(cert_key) is not SecretStr:
            raise TypeError("cert_key must be an exact SecretStr or None")
        if now is not None and not callable(now):
            raise TypeError("now must be callable or None")
        if (
            type(cache_ttl_seconds) is not float
            or not isfinite(cache_ttl_seconds)
            or not 0.0 < cache_ttl_seconds <= 86_400.0
        ):
            raise ValueError("cache_ttl_seconds must be finite and in (0, 86400]")
        self._cert_key = cert_key
        self._now = now or (lambda: datetime.now(UTC))
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[FuelType, FuelPrice] = {}
        self._cache_expires_at: datetime | None = None
        self._lock = asyncio.Lock()
        self._transport = BoundedHttpClient(
            http=http,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
        )
        self._http = self._transport.http

    async def average_price(self, fuel_type: FuelType) -> FuelPrice:
        if type(fuel_type) is not FuelType:
            raise TypeError("fuel_type must be an exact FuelType")
        current = self._current_time()
        cached = self._cache.get(fuel_type)
        if (
            cached is not None
            and self._cache_expires_at is not None
            and current < self._cache_expires_at
        ):
            return cached
        async with self._lock:
            current = self._current_time()
            cached = self._cache.get(fuel_type)
            if (
                cached is not None
                and self._cache_expires_at is not None
                and current < self._cache_expires_at
            ):
                return cached
            payload = await self._transport.get_json(
                url=_AVERAGE_URL,
                params={"out": "json"},
                query_secret=("certkey", self._cert_key),
            )
            prices = _parse_prices(payload)
            if fuel_type not in prices:
                raise ProviderRequestError(
                    "SCHEMA_MISMATCH",
                    "Opinet response omitted a required official product code",
                )
            self._cache = prices
            self._cache_expires_at = current + timedelta(
                seconds=self._cache_ttl_seconds
            )
            return prices[fuel_type]

    async def aclose(self) -> None:
        await self._transport.aclose()

    @property
    def last_status_code(self) -> int | None:
        return self._transport.last_status_code

    @property
    def last_schema_fingerprint(self) -> str | None:
        return self._transport.last_schema_fingerprint

    def _current_time(self) -> datetime:
        value = self._now()
        if type(value) is not datetime or value.tzinfo is None:
            raise ProviderRequestError(
                "CLOCK_INVALID", "Opinet clock did not return an aware datetime"
            )
        return value


def estimate_car_cost(
    *,
    distance_meters: int,
    fuel_price_krw_per_liter: float,
    assumptions: CarAssumptions,
    toll_krw: int,
) -> RouteCostBreakdown:
    if type(distance_meters) is not int:
        raise TypeError("distance_meters must be an exact int")
    if distance_meters < 0:
        raise ValueError("distance_meters must be nonnegative")
    if type(fuel_price_krw_per_liter) is not float:
        raise TypeError("fuel price must be an exact float")
    if not isfinite(fuel_price_krw_per_liter) or fuel_price_krw_per_liter <= 0.0:
        raise ValueError("fuel price must be positive and finite")
    if type(assumptions) is not CarAssumptions:
        raise TypeError("assumptions must be an exact CarAssumptions")
    if type(toll_krw) is not int:
        raise TypeError("toll_krw must be an exact int")
    if toll_krw < 0:
        raise ValueError("toll_krw must be nonnegative")
    liters = (distance_meters / 1_000.0) / assumptions.efficiency_km_per_liter
    fuel_krw = round(liters * fuel_price_krw_per_liter)
    return RouteCostBreakdown(
        fuel_krw=fuel_krw,
        toll_krw=toll_krw,
        parking_krw=assumptions.parking_cost_krw,
    )


def _parse_prices(payload: dict[str, object]) -> dict[FuelType, FuelPrice]:
    try:
        result = payload.get("RESULT")
        if type(result) is not dict:
            raise ValueError
        oils = result.get("OIL")
        if type(oils) is not list or not 1 <= len(oils) <= 20:
            raise ValueError
        by_code = {code: fuel for fuel, code in _PRODUCT_CODES.items()}
        prices: dict[FuelType, FuelPrice] = {}
        for oil in oils:
            if type(oil) is not dict:
                raise ValueError
            code = oil.get("PRODCD")
            if code not in by_code:
                continue
            trade = oil.get("TRADE_DT")
            raw_price = oil.get("PRICE")
            if type(trade) is not str or len(trade) != 8 or not trade.isascii():
                raise ValueError
            if type(raw_price) is not str or not raw_price.strip():
                raise ValueError
            price = float(raw_price)
            if not isfinite(price) or price <= 0.0:
                raise ValueError
            fuel = by_code[code]
            if fuel in prices:
                raise ValueError
            prices[fuel] = FuelPrice(
                fuel_type=fuel,
                krw_per_liter=price,
                trade_date=date.fromisoformat(
                    f"{trade[0:4]}-{trade[4:6]}-{trade[6:8]}"
                ),
            )
        if set(prices) != set(_PRODUCT_CODES):
            raise ValueError
        return prices
    except (TypeError, ValueError, OverflowError):
        raise ProviderRequestError(
            "SCHEMA_MISMATCH",
            "Opinet response did not match the documented average-price schema",
        ) from None
