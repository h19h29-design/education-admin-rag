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
