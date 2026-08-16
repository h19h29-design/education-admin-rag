import json


# Break caught: exposing the server credential instead of only the browser-restricted key.
def test_bootstrap_exposes_only_domain_restricted_javascript_key(client) -> None:
    response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body)
    assert body["map"]["javascriptKey"] == "public-js-key"
    assert "rest-secret" not in serialized
    assert "seoul-secret" not in serialized
