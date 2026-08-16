"""Small in-process TTL/LRU cache for public provider results."""

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import floor
from time import monotonic
from typing import Generic, TypeVar

from app.routing.models import Coordinate, TravelMode

T = TypeVar("T")

PLACES_TTL_SECONDS = 86_400.0
WALK_TTL_SECONDS = 604_800.0
CAR_AND_TRANSIT_TTL_SECONDS = 300.0
FUEL_TTL_SECONDS = 86_400.0


@dataclass(frozen=True)
class _Entry(Generic[T]):
    value: T
    expires_at: float


class TtlLruCache:
    def __init__(
        self,
        *,
        max_entries: int,
        now: Callable[[], float] = monotonic,
    ) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 10_000:
            raise ValueError("max_entries must be an integer in [1, 10000]")
        if not callable(now):
            raise TypeError("now must be callable")
        self._max_entries = max_entries
        self._now = now
        self._entries: OrderedDict[str, _Entry[object]] = OrderedDict()

    def get(self, key: str) -> object | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if self._now() >= entry.expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: T, *, ttl_seconds: float) -> T:
        if type(key) is not str or not key:
            raise ValueError("cache key must be nonblank")
        if type(ttl_seconds) is not float or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive float")
        self._entries[key] = _Entry(value=value, expires_at=self._now() + ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return value

    def route_key(
        self,
        *,
        provider: str,
        mode: TravelMode,
        origin: Coordinate,
        destination: Coordinate,
        depart_at: datetime | str,
        options: Mapping[str, object],
    ) -> str:
        if type(provider) is not str or not provider.strip():
            raise ValueError("provider must be nonblank")
        if type(mode) is not TravelMode:
            raise TypeError("mode must be TravelMode")
        departure_bucket = _departure_bucket(depart_at)
        payload = {
            "provider": provider,
            "mode": mode.value,
            "origin": _quantized_coordinate(origin),
            "destination": _quantized_coordinate(destination),
            "departureBucket": departure_bucket,
            "options": options,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def key(namespace: str, payload: Mapping[str, object]) -> str:
        encoded = json.dumps(
            {"namespace": namespace, "payload": payload},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _quantized_coordinate(value: Coordinate) -> tuple[str, str]:
    return (f"{value.latitude:.5f}", f"{value.longitude:.5f}")


def _departure_bucket(value: datetime | str) -> int | str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("depart_at must be timezone-aware")
        return floor(value.timestamp() / 300.0)
    if type(value) is str and value:
        return value
    raise TypeError("depart_at must be an aware datetime or nonblank string")
