pytest_plugins = ("tests.api.conftest",)


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
