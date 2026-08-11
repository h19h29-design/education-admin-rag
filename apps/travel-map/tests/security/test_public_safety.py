import logging
from collections.abc import AsyncIterator

import httpx
import pytest
from app.main import _MAX_REQUEST_BYTES, app, create_app
from app.settings import Settings
from fastapi.testclient import TestClient

pytest_plugins = ("tests.api.conftest",)


class _OversizedChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"x" * (_MAX_REQUEST_BYTES + 1)

    async def aclose(self) -> None:
        return None


# Break caught: a body-limit middleware allowing an unread chunked body through
# when the downstream endpoint does not call receive().
@pytest.mark.anyio
async def test_request_limit_rejects_declared_and_unread_chunked_bodies() -> None:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        declared = await client.request(
            "GET", "/healthz", content=b"x" * (_MAX_REQUEST_BYTES + 1)
        )
        chunked_request = client.build_request(
            "GET", "/healthz", content=_OversizedChunkedBody()
        )
        assert "content-length" not in chunked_request.headers
        chunked = await client.send(chunked_request)

    for response in (declared, chunked):
        assert response.status_code == 413
        assert response.json() == {"error": {"code": "REQUEST_TOO_LARGE"}}


# Break caught: an invalid Host response bypassing FastAPI exception handlers and
# returning Starlette's plaintext body instead of the public JSON error envelope.
def test_invalid_host_uses_json_error_envelope(client) -> None:
    response = client.get("/healthz", headers={"Host": "untrusted.example"})

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "INVALID_HOST"}}


# Break caught: serving the public UI without a browser-enforced policy left
# response-backed map labels able to execute through an HTML rendering sink.
def test_public_ui_has_strict_kakao_compatible_content_security_policy() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == (
        "default-src 'self'; "
        "script-src 'self' https://dapi.kakao.com https://t1.daumcdn.net; "
        "style-src 'self'; "
        "img-src 'self' data: https://*.daumcdn.net; "
        "connect-src 'self' https://dapi.kakao.com; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    )
    assert "unsafe-inline" not in response.headers["content-security-policy"]


# Break caught: CORS preflight short-circuiting before exact Host validation and
# accepting an untrusted host with a configured browser origin.
def test_invalid_host_is_rejected_before_cors_preflight(client) -> None:
    response = client.options(
        "/api/v1/places",
        headers={
            "Host": "untrusted.example",
            "Origin": "https://travel.example.test",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "INVALID_HOST"}}
    assert "access-control-allow-origin" not in response.headers


# Break caught: an enormous all-digit Content-Length reaching Python's integer
# conversion limit and turning a malformed request into a plaintext 500.
@pytest.mark.anyio
async def test_request_limit_rejects_enormous_numeric_content_length_as_json() -> None:
    transport = httpx.ASGITransport(app=create_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/healthz", headers={"Content-Length": "9" * 5_000})

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "INVALID_CONTENT_LENGTH"}}


# Break caught: the Docker/default `app.main:app` module only installing the
# Uvicorn access-log redaction when a separate production app is constructed.
def test_default_module_app_redacts_uvicorn_query_string() -> None:
    logger = logging.getLogger("uvicorn.access")
    handler = _CollectingHandler()
    original_level = logger.level
    original_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        assert app.title == "서울교육기관 관내출장 지도"
        logger.info(
            '%s - "%s %s HTTP/%s" %s',
            "127.0.0.1:12345",
            "GET",
            "/healthz?private-query",
            "1.1",
            200,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.disabled = original_disabled

    assert len(handler.records) == 1
    message = handler.records[0].getMessage()
    assert "/healthz" in message
    assert "private-query" not in message
    assert "?" not in message


# Break caught: Uvicorn's production access formatter receiving a query-bearing
# request target even though the application logger is query-free.
def test_production_uvicorn_access_log_redacts_query_and_keeps_path_and_status() -> (
    None
):
    settings = Settings(
        environment="production",
        kakao_rest_api_key="rest-secret",
        seoul_transit_service_key="seoul-secret",
        opinet_cert_key="opinet-secret",
        allowed_hosts=("travel.example.test",),
        allowed_origins=("https://travel.example.test",),
    )
    logger = logging.getLogger("uvicorn.access")
    handler = _CollectingHandler()
    original_level = logger.level
    original_disabled = logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        create_app(settings)
        logger.info(
            '%s - "%s %s HTTP/%s" %s',
            "127.0.0.1:12345",
            "GET",
            "/api/v1/places?q=private-query",
            "1.1",
            200,
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
        logger.disabled = original_disabled

    assert len(handler.records) == 1
    message = handler.records[0].getMessage()
    assert "/api/v1/places" in message
    assert "200" in message
    assert "private-query" not in message
    assert "?" not in message


class _CollectingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
