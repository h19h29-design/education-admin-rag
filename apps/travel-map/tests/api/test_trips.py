from app.cache import WALK_TTL_SECONDS
from tests.api.conftest import trip_payload


# Break caught: trusting caller coordinates for an institution origin instead of its verified site id.
def test_trip_preview_resolves_origin_by_site_id_and_separates_costs(client) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["origin"]["siteId"] == "test-neis:B10:SEMWATER-ES:main"
    assert body["best"]["fastestRouteId"]
    assert body["mobilityCost"] != body["allowance"]
    assert body["allowance"]["amountKrw"] == 20_000


def test_trip_preview_rejects_caller_supplied_origin_coordinates(client) -> None:
    payload = trip_payload()
    payload["origin"] = {"latitude": 35.0, "longitude": 129.0}

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 422


def test_trip_preview_rejects_snake_case_request_fields(client) -> None:
    payload = trip_payload()
    payload["previous_allowance_krw"] = payload.pop("previousAllowanceKrw")

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 422


def test_trip_preview_accepts_camel_case_request_fields(client) -> None:
    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200


def test_same_day_remaining_allowance_never_exceeds_the_daily_cap(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            hasOtherLocalTripsToday=True,
            previousAllowanceKrw=10_000,
        ),
    )

    assert response.status_code == 200
    assert response.json()["allowance"]["amountKrw"] == 10_000

    exhausted = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(
            hasOtherLocalTripsToday=True,
            previousAllowanceKrw=20_000,
        ),
    )

    assert exhausted.status_code == 200
    assert exhausted.json()["allowance"]["amountKrw"] == 0


# Break caught: contacting route providers after an out-of-coverage destination has been determined.
def test_outside_coverage_stops_before_provider_calls(client, fake_provider) -> None:
    payload = trip_payload(
        destination={
            "name": "부산광역시청",
            "address": "부산광역시 연제구 중앙대로 1001",
            "latitude": 35.1798159,
            "longitude": 129.0750222,
        }
    )

    response = client.post("/api/v1/trips/preview", json=payload)

    assert response.status_code == 200
    assert response.json()["coverage"]["status"] == "OUT_OF_COVERAGE"
    assert response.json()["routes"] == []
    assert fake_provider.call_count == 0


# Break caught: assigning a flat allowance when the employment status is unverified.
def test_unknown_profile_returns_routes_but_withholds_allowance(client) -> None:
    response = client.post(
        "/api/v1/trips/preview",
        json=trip_payload(policyProfile="NONPUBLIC_OR_UNKNOWN"),
    )

    assert response.status_code == 200
    assert response.json()["routes"]
    assert response.json()["allowance"]["status"] == "REVIEW_REQUIRED"
    assert response.json()["allowance"]["amountKrw"] is None


# Break caught: repeating an identical preview invokes display route providers inside
# their five-minute cache window.
def test_trip_preview_caches_display_routes(client, fake_provider) -> None:
    first = client.post("/api/v1/trips/preview", json=trip_payload())
    second = client.post("/api/v1/trips/preview", json=trip_payload())

    assert first.status_code == second.status_code == 200
    assert fake_provider.call_count == 1


# Break caught: a combined five-minute display cache expiring walk results with
# car/transit, instead of retaining the walk mode for its seven-day policy.
def test_trip_preview_keeps_walk_cached_after_car_and_transit_expire(
    client, cache_clock
) -> None:
    first = client.post("/api/v1/trips/preview", json=trip_payload())
    cache_clock[0] += 301.0
    second = client.post("/api/v1/trips/preview", json=trip_payload())

    assert first.status_code == second.status_code == 200
    first_ids = {route["mode"]: route["id"] for route in first.json()["routes"]}
    second_ids = {route["mode"]: route["id"] for route in second.json()["routes"]}
    assert second_ids["WALK"] == first_ids["WALK"]
    assert second_ids["CAR"] != first_ids["CAR"]
    assert second_ids["TRANSIT"] != first_ids["TRANSIT"]

    cache_clock[0] = WALK_TTL_SECONDS + 1.0
    expired = client.post("/api/v1/trips/preview", json=trip_payload())
    expired_ids = {route["mode"]: route["id"] for route in expired.json()["routes"]}
    assert expired.status_code == 200
    assert expired_ids["WALK"] != first_ids["WALK"]


# Break caught: using the displayed route or one-way distance for the legal two-kilometre branch.
def test_seoul_destination_uses_two_directional_distance_for_two_km_branch(
    client, fake_classification_provider
) -> None:
    fake_classification_provider.set_directional_distances(900, 1_100)

    response = client.post("/api/v1/trips/preview", json=trip_payload())

    assert response.status_code == 200
    body = response.json()
    assert body["classification"] == "LOCAL"
    assert body["classificationDistanceMeters"] == 2_000
    assert body["allowance"]["status"] == "REVIEW_REQUIRED"
    assert body["allowance"]["amountKrw"] is None
    assert [query.origin for query in fake_classification_provider.queries] == [
        fake_classification_provider.site_coordinate,
        fake_classification_provider.destination_coordinate,
    ]
