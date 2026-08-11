"""Per-client fixed-window limits for the anonymous public API."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import ceil, floor
from time import monotonic


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: int | None = None


class FixedWindowRateLimiter:
    def __init__(
        self,
        *,
        limits: Mapping[str, tuple[int, float]],
        now: Callable[[], float] = monotonic,
    ) -> None:
        if not callable(now):
            raise TypeError("now must be callable")
        normalized: dict[str, tuple[int, float]] = {}
        for scope, limit in limits.items():
            if type(scope) is not str or not scope.strip():
                raise ValueError("rate-limit scope must be nonblank")
            if type(limit) is not tuple or len(limit) != 2:
                raise TypeError("rate-limit values must be (count, window) tuples")
            count, window = limit
            if type(count) is not int or count < 1:
                raise ValueError("rate-limit count must be a positive integer")
            if type(window) is not float or window <= 0.0:
                raise ValueError("rate-limit window must be a positive float")
            normalized[scope] = (count, window)
        self._limits = normalized
        self._now = now
        self._counts: dict[tuple[str, str, int], int] = {}

    def check(self, scope: str, client_ip: str) -> RateLimitDecision:
        if type(scope) is not str or type(client_ip) is not str or not client_ip:
            raise TypeError("scope and client_ip must be nonblank strings")
        count, window = self._limits[scope]
        now = self._now()
        bucket = floor(now / window)
        key = (scope, client_ip, bucket)
        seen = self._counts.get(key, 0)
        if seen >= count:
            remaining = window - (now - bucket * window)
            return RateLimitDecision(False, max(1, ceil(remaining)))
        self._counts[key] = seen + 1
        return RateLimitDecision(True)
