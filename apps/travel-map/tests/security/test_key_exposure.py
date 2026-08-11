import json

pytest_plugins = ("tests.api.conftest",)


# Break caught: a public API payload containing a REST or Seoul transit credential.
def test_public_responses_do_not_expose_server_credentials(client) -> None:
    response = client.get("/api/v1/bootstrap")

    assert response.status_code == 200
    serialized = json.dumps(response.json())
    assert "rest-secret" not in serialized
    assert "seoul-secret" not in serialized
    assert "opinet-secret" not in serialized
