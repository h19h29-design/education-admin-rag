from app.cache import PLACES_TTL_SECONDS, TtlLruCache
from app.routing.models import Coordinate, TravelMode


# Break caught: repeated equivalent place searches calling the provider instead of cache.
def test_places_search_is_cached(client, fake_place_client) -> None:
    first = client.get("/api/v1/places", params={"q": "서울시청"})
    second = client.get("/api/v1/places", params={"q": "서울시청"})

    assert first.status_code == second.status_code == 200
    assert first.json()["items"][0]["placeId"] == "fixture-city-hall"
    assert fake_place_client.search_calls == 1


def test_reverse_places_returns_a_public_place_candidate(
    client, fake_place_client
) -> None:
    response = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )

    assert response.status_code == 200
    assert response.json()["item"]["placeId"] == "fixture-reverse"
    assert fake_place_client.reverse_calls == 1


# Break caught: reverse geocoding repeatedly calls the provider, or merges distinct
# coordinates, instead of using an exact 24-hour cache key.
def test_reverse_places_uses_an_exact_coordinate_cache(
    client, fake_place_client, cache_clock
) -> None:
    first = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    repeated = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    nearby = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662953, "longitude": 126.9779451},
    )

    assert first.json() == repeated.json()
    assert fake_place_client.reverse_calls == 2
    assert nearby.status_code == 200

    cache_clock[0] = PLACES_TTL_SECONDS
    expired = client.get(
        "/api/v1/places/reverse",
        params={"latitude": 37.5662952, "longitude": 126.9779451},
    )
    assert expired.status_code == 200
    assert fake_place_client.reverse_calls == 3


# Break caught: cache keys using raw coordinates and treating requests within 1e-5 apart as distinct.
def test_route_cache_key_quantizes_coordinates_to_five_decimals() -> None:
    cache = TtlLruCache(max_entries=2)
    first = cache.route_key(
        provider="FAKE",
        mode=TravelMode.CAR,
        origin=Coordinate(37.5000001, 126.9000001),
        destination=Coordinate(37.6000001, 127.0000001),
        depart_at="2026-08-10T09:00:00+09:00",
        options={"priority": "DISTANCE"},
    )
    second = cache.route_key(
        provider="FAKE",
        mode=TravelMode.CAR,
        origin=Coordinate(37.5000002, 126.9000002),
        destination=Coordinate(37.6000002, 127.0000002),
        depart_at="2026-08-10T09:00:00+09:00",
        options={"priority": "DISTANCE"},
    )

    assert first == second
