# Break caught: returning a private snapshot record or snake_case API field names.
def test_institutions_search_returns_public_camel_case_records(client) -> None:
    response = client.get("/api/v1/institutions", params={"q": "샘물"})

    assert response.status_code == 200
    body = response.json()
    assert "test-neis:B10:SEMWATER-ES:main" in {
        item["siteId"] for item in body["items"]
    }
    assert "site_id" not in body["items"][0]
