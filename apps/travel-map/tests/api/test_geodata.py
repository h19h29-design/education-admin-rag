def test_geodata_returns_validated_geojson_with_a_stable_etag(client) -> None:
    response = client.get("/api/v1/geodata/seoul")

    assert response.status_code == 200
    assert response.json()["type"] == "FeatureCollection"
    assert response.headers["ETag"].startswith('"')
    assert "public" in response.headers["Cache-Control"]

    not_modified = client.get(
        "/api/v1/geodata/seoul",
        headers={"If-None-Match": response.headers["ETag"]},
    )
    assert not_modified.status_code == 304


def test_support_geodata_is_available_without_provider_credentials(client) -> None:
    response = client.get("/api/v1/geodata/support")

    assert response.status_code == 200
    assert response.json()["features"]
