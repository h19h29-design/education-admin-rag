import asyncio
from collections.abc import Mapping
from types import TracebackType

import httpx
import pytest
from app.providers.http import BoundedHttpClient, ProviderRequestError
from app.providers.opinet import OpinetClient
from app.routing.models import FuelType
from pydantic import SecretStr


def _contains_credential_material(
    value: object,
    secret: str,
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> bool:
    if seen is None:
        seen = set()
    if depth > 7 or id(value) in seen:
        return False
    seen.add(id(value))
    if isinstance(value, SecretStr):
        return True
    if isinstance(value, str):
        return secret in value
    if isinstance(value, bytes):
        return secret.encode() in value
    if isinstance(value, Mapping):
        return any(
            _contains_credential_material(item, secret, seen=seen, depth=depth + 1)
            for pair in value.items()
            for item in pair
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(
            _contains_credential_material(item, secret, seen=seen, depth=depth + 1)
            for item in value
        )
    if isinstance(value, httpx.Request):
        return _contains_credential_material(
            (str(value.url), dict(value.headers)),
            secret,
            seen=seen,
            depth=depth + 1,
        )
    return False


def _assert_provider_traceback_is_credential_free(
    traceback_value: TracebackType | None,
    secret: str,
) -> None:
    current = traceback_value
    while current is not None:
        filename = current.tb_frame.f_code.co_filename
        if "/app/providers/" in filename or "/site-packages/httpx/" in filename:
            assert not _contains_credential_material(
                current.tb_frame.f_locals,
                secret,
            ), filename
        current = current.tb_next


@pytest.mark.asyncio
async def test_transport_failure_traceback_has_no_secret_or_secretstr_local() -> None:
    secret = "header-trace-material"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        boundary = BoundedHttpClient(
            http=http,
            timeout_seconds=5.0,
            max_response_bytes=1_000,
        )
        with pytest.raises(ProviderRequestError) as raised:
            await boundary.get_json(
                url="https://example.invalid/data",
                params={},
                header_secret=SecretStr(secret),
            )

    _assert_provider_traceback_is_credential_free(raised.tb, secret)


@pytest.mark.asyncio
async def test_query_credential_cancellation_exposes_only_sanitized_boundary() -> None:
    secret = "query-cancel-material"
    started = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpinetClient(http=http, cert_key=SecretStr(secret))
        task = asyncio.create_task(client.average_price(FuelType.GASOLINE))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task

    _assert_provider_traceback_is_credential_free(raised.tb, secret)


# Break caught: extracting a credential before rejecting malformed public params.
@pytest.mark.asyncio
async def test_nonsecret_request_fields_are_validated_before_credentials() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        boundary = BoundedHttpClient(
            http=http,
            timeout_seconds=5.0,
            max_response_bytes=1_000,
        )
        with pytest.raises(TypeError, match="params"):
            boundary.get_json(
                url="https://example.invalid/data",
                params={"page": 1},  # type: ignore[dict-item]
                header_secret=object(),  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="query_secret"):
            await boundary.get_json(
                url="https://example.invalid/data",
                params={},
                query_secret=("", SecretStr("secret")),
            )


# Break caught: a nested upstream field drifts without changing the schema fingerprint.
@pytest.mark.asyncio
async def test_schema_fingerprint_tracks_nested_upstream_shape_without_values() -> None:
    payloads = [
        {"routes": [{"summary": {"distance": 10}}]},
        {"routes": [{"summary": {"distance": 99, "duration": 20}}]},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=payloads.pop(0),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        boundary = BoundedHttpClient(
            http=http,
            timeout_seconds=5.0,
            max_response_bytes=1_000,
        )
        await boundary.get_json(
            url="https://example.invalid/data",
            params={},
            header_secret=SecretStr("secret"),
        )
        first = boundary.last_schema_fingerprint
        await boundary.get_json(
            url="https://example.invalid/data",
            params={},
            header_secret=SecretStr("secret"),
        )
        second = boundary.last_schema_fingerprint

    assert type(first) is str and len(first) == 64
    assert type(second) is str and len(second) == 64
    assert first != second
    assert "10" not in first and "99" not in second


@pytest.mark.asyncio
async def test_owned_close_can_retry_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = BoundedHttpClient(
        http=None,
        timeout_seconds=5.0,
        max_response_bytes=1_000,
    )
    original_close = boundary.http.aclose
    calls = 0

    async def flaky_close() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("close failed")
        await original_close()

    monkeypatch.setattr(boundary.http, "aclose", flaky_close)

    with pytest.raises(RuntimeError, match="close failed"):
        await boundary.aclose()
    await boundary.aclose()
    await boundary.aclose()

    assert calls == 2
    assert boundary.http.is_closed


@pytest.mark.asyncio
async def test_owned_close_can_retry_after_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary = BoundedHttpClient(
        http=None,
        timeout_seconds=5.0,
        max_response_bytes=1_000,
    )
    original_close = boundary.http.aclose
    started = asyncio.Event()
    calls = 0

    async def cancellable_close() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        await original_close()

    monkeypatch.setattr(boundary.http, "aclose", cancellable_close)
    task = asyncio.create_task(boundary.aclose())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await boundary.aclose()

    assert calls == 2
    assert boundary.http.is_closed
