# Break caught: returning a private snapshot record or snake_case API field names.
def test_institutions_search_returns_public_camel_case_records(client) -> None:
    response = client.get("/api/v1/institutions", params={"q": "샘물"})

    assert response.status_code == 200
    body = response.json()
    assert "test-neis:B10:SEMWATER-ES:main" in {
        item["siteId"] for item in body["items"]
    }
    assert "site_id" not in body["items"][0]


def test_institutions_search_applies_normalized_type_and_foundation_filters(client) -> None:
    response = client.get(
        "/api/v1/institutions",
        params={
            "q": "샘물",
            "institution_type": "ELEMENTARY_SCHOOL",
            "foundation_type": "PUBLIC",
        },
    )

    assert response.status_code == 200
    assert [item["siteId"] for item in response.json()["items"]] == [
        "test-neis:B10:SEMWATER-ES:main"
    ]


# Break caught: leaking a review-required school through the public API despite
# its valid stored coordinates, either by name or by supported filters.
def test_institutions_api_excludes_review_required_school_from_name_and_filters(
    client,
) -> None:
    by_name = client.get("/api/v1/institutions", params={"q": "검토학교"})
    by_filter = client.get(
        "/api/v1/institutions",
        params={"institution_type": "ELEMENTARY_SCHOOL"},
    )

    assert by_name.status_code == 200
    assert by_name.json()["items"] == []
    assert by_filter.status_code == 200
    assert "test-neis:B10:REVIEW-PARENT:main" not in {
        item["siteId"] for item in by_filter.json()["items"]
    }
