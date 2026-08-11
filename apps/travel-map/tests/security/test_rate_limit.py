pytest_plugins = ("tests.api.conftest",)

from app.rate_limit import FixedWindowRateLimiter


# Break caught: an unauthenticated places endpoint accepting more than its fixed-window budget.
def test_rate_limit_returns_retry_after(client) -> None:
    for _ in range(10):
        assert client.get("/api/v1/places", params={"q": "서울시청"}).status_code in {
            200,
            503,
        }
    blocked = client.get("/api/v1/places", params={"q": "서울시청"})

    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


# Break caught: fixed-window buckets persisting after their window expires and
# allowing rate-limit state to grow without a time-based bound.
def test_rate_limiter_discards_stale_windows() -> None:
    clock = [0.0]
    limiter = FixedWindowRateLimiter(
        limits={"places": (10, 60.0)}, now=lambda: clock[0]
    )

    for moment in (0.0, 60.0, 120.0):
        clock[0] = moment
        assert limiter.check("places", "198.51.100.10").allowed

    assert len(limiter._counts) == 1


# Break caught: a client fan-out filling an active fixed window without a hard
# bucket capacity, creating an unbounded in-process memory commitment.
def test_rate_limiter_rejects_new_clients_when_bucket_capacity_is_reached() -> None:
    limiter = FixedWindowRateLimiter(
        limits={"places": (10, 60.0)},
        max_buckets=2,
    )

    assert limiter.check("places", "198.51.100.1").allowed
    assert limiter.check("places", "198.51.100.2").allowed
    rejected = limiter.check("places", "198.51.100.3")

    assert not rejected.allowed
    assert rejected.retry_after_seconds is not None
