import asyncio
import hashlib
import json
from collections.abc import Coroutine, Mapping
from math import isfinite
from typing import Any, TypeVar, cast

import httpx
from pydantic import SecretStr

from app.routing.models import ProviderWarning

_T = TypeVar("_T")
_UNSET = object()


class ProviderRequestError(RuntimeError):
    """A sanitized provider failure safe to expose as a warning."""

    def __init__(self, code: str, message: str) -> None:
        if type(code) is not str or not code.strip():
            raise TypeError("provider error code must be a nonblank string")
        if type(message) is not str or not message.strip():
            raise TypeError("provider error message must be a nonblank string")
        self.code = code
        super().__init__(message)

    def warning(self, source: str) -> ProviderWarning:
        return ProviderWarning(code=self.code, message=str(self), source=source)


class BoundedHttpClient:
    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None,
        timeout_seconds: float,
        max_response_bytes: int,
        max_attempts: int = 2,
    ) -> None:
        if http is not None and type(http) is not httpx.AsyncClient:
            raise TypeError("http must be an exact AsyncClient or None")
        if (
            type(timeout_seconds) is not float
            or not isfinite(timeout_seconds)
            or not 0.0 < timeout_seconds <= 30.0
        ):
            raise ValueError("timeout_seconds must be finite and in (0, 30]")
        if (
            type(max_response_bytes) is not int
            or not 1 <= max_response_bytes <= 5_000_000
        ):
            raise ValueError("max_response_bytes must be in [1, 5000000]")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts must be in [1, 3]")
        self.http = (
            http if http is not None else httpx.AsyncClient(follow_redirects=False)
        )
        self.owns_http = http is None
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_attempts = max_attempts
        self.last_status_code: int | None = None
        self.last_schema_fingerprint: str | None = None
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            if self.owns_http:
                await self.http.aclose()
            self._closed = True

    def get_json(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        header_secret: SecretStr | None = None,
        query_secret: tuple[str, SecretStr | None] | None = None,
    ) -> Coroutine[Any, Any, dict[str, Any]]:
        self.last_status_code = None
        self.last_schema_fingerprint = None
        accepted_content_types = frozenset({"application/json"})
        _validate_request_fields(
            url=url,
            params=params,
            accepted_content_types=accepted_content_types,
        )
        credential_kind, credential_name, credential = _extract_credential(
            header_secret=header_secret,
            query_secret=query_secret,
        )
        task = asyncio.create_task(
            self._json_worker(
                url=url,
                params=params,
                accepted_content_types=accepted_content_types,
                credential_kind=credential_kind,
                credential_name=credential_name,
                credential=credential,
            )
        )
        credential = ""
        return cast("Coroutine[Any, Any, dict[str, Any]]", self._await_task(task))

    def get_xml(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        query_secret: tuple[str, SecretStr | None],
    ) -> Coroutine[Any, Any, bytes]:
        self.last_status_code = None
        self.last_schema_fingerprint = None
        accepted_content_types = frozenset(
            {"application/xml", "text/xml", "application/xhtml+xml"}
        )
        _validate_request_fields(
            url=url,
            params=params,
            accepted_content_types=accepted_content_types,
        )
        credential_kind, credential_name, credential = _extract_credential(
            header_secret=None,
            query_secret=query_secret,
        )
        task = asyncio.create_task(
            self._request_worker(
                url=url,
                params=params,
                accepted_content_types=accepted_content_types,
                credential_kind=credential_kind,
                credential_name=credential_name,
                credential=credential,
            )
        )
        credential = ""
        return self._await_task(task)

    async def _json_worker(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        accepted_content_types: frozenset[str],
        credential_kind: str,
        credential_name: str,
        credential: str,
    ) -> dict[str, Any]:
        raw = await self._request_worker(
            url=url,
            params=params,
            accepted_content_types=accepted_content_types,
            credential_kind=credential_kind,
            credential_name=credential_name,
            credential=credential,
        )
        credential = ""
        failure = False
        value: object | None = None
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            if type(value) is not dict:
                failure = True
        except (UnicodeDecodeError, ValueError):
            failure = True
        finally:
            raw = b""
        if failure or type(value) is not dict:
            value = None
            raise ProviderRequestError(
                "SCHEMA_MISMATCH",
                "Provider response did not match the documented JSON schema",
            ) from None
        self.last_schema_fingerprint = _schema_fingerprint(value)
        return value

    async def _await_task(self, task: asyncio.Task[_T]) -> _T:
        result: _T | object = _UNSET
        failure: tuple[str, str] | None = None
        cancelled = False
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except ProviderRequestError as exc:
            failure = (exc.code, str(exc))
            exc.__traceback__ = None
            exc.__context__ = None
            exc.__cause__ = None

        if cancelled:
            task.cancel()
            await _consume_task(task)
            task = cast("asyncio.Task[_T]", None)
            raise asyncio.CancelledError from None
        if failure is not None:
            task = cast("asyncio.Task[_T]", None)
            raise ProviderRequestError(*failure) from None
        if result is _UNSET:
            raise RuntimeError("provider task completed without a result")
        return cast("_T", result)

    async def _request_worker(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        accepted_content_types: frozenset[str],
        credential_kind: str,
        credential_name: str,
        credential: str,
    ) -> bytes:
        params_buffer = dict(params)
        headers: dict[str, str] = {}
        body = bytearray()
        failure: tuple[str, str] | None = None
        try:
            if credential_kind == "header":
                headers[credential_name] = f"KakaoAK {credential}"
            else:
                params_buffer[credential_name] = credential
            credential = ""

            for attempt in range(self.max_attempts):
                body.clear()
                try:
                    async with self.http.stream(
                        "GET",
                        url,
                        params=params_buffer,
                        headers=headers,
                        timeout=httpx.Timeout(self.timeout_seconds),
                        follow_redirects=False,
                    ) as response:
                        status = response.status_code
                        self.last_status_code = status
                        if status == 429:
                            if attempt + 1 < self.max_attempts:
                                continue
                            failure = (
                                "UPSTREAM_RATE_LIMIT",
                                "Provider rate limit was reached",
                            )
                            break
                        if status >= 500:
                            if attempt + 1 < self.max_attempts:
                                continue
                            failure = (
                                "UPSTREAM_UNAVAILABLE",
                                "Provider service is temporarily unavailable",
                            )
                            break
                        if status >= 400:
                            failure = (
                                "UPSTREAM_REJECTED",
                                "Provider rejected the request",
                            )
                            break
                        content_type = response.headers.get("Content-Type", "")
                        media_type = content_type.split(";", 1)[0].strip().lower()
                        if media_type not in accepted_content_types:
                            failure = (
                                "SCHEMA_MISMATCH",
                                "Provider response content type was unexpected",
                            )
                            break
                        length = response.headers.get("Content-Length")
                        if length is not None:
                            try:
                                declared_length = int(length)
                            except ValueError:
                                failure = (
                                    "SCHEMA_MISMATCH",
                                    "Provider response length was invalid",
                                )
                                break
                            if declared_length < 0:
                                failure = (
                                    "SCHEMA_MISMATCH",
                                    "Provider response length was invalid",
                                )
                                break
                            if declared_length > self.max_response_bytes:
                                failure = (
                                    "RESPONSE_TOO_LARGE",
                                    "Provider response exceeded the byte limit",
                                )
                                break
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > self.max_response_bytes:
                                failure = (
                                    "RESPONSE_TOO_LARGE",
                                    "Provider response exceeded the byte limit",
                                )
                                break
                            body.extend(chunk)
                        break
                except asyncio.CancelledError:
                    raise
                except httpx.TimeoutException:
                    if attempt + 1 == self.max_attempts:
                        failure = ("UPSTREAM_TIMEOUT", "Provider request timed out")
                except httpx.RequestError:
                    if attempt + 1 == self.max_attempts:
                        failure = ("UPSTREAM_ERROR", "Provider request failed")
                except Exception:  # noqa: BLE001
                    failure = ("UPSTREAM_ERROR", "Provider request failed")
                    break
        finally:
            credential = ""
            params_buffer.clear()
            headers.clear()

        if failure is not None:
            body.clear()
            raise ProviderRequestError(*failure) from None
        return bytes(body)


async def _consume_task(task: asyncio.Task[object]) -> None:
    try:
        await task
    except BaseException as exc:  # noqa: BLE001
        exc.__traceback__ = None
        exc.__context__ = None
        exc.__cause__ = None


def _extract_credential(
    *,
    header_secret: SecretStr | None,
    query_secret: tuple[str, SecretStr | None] | None,
) -> tuple[str, str, str]:
    if header_secret is None and query_secret is None:
        raise ProviderRequestError(
            "MISSING_CREDENTIAL",
            "Provider credential is unavailable",
        )
    if header_secret is not None:
        if type(header_secret) is not SecretStr or query_secret is not None:
            raise TypeError("exactly one exact provider credential is required")
        credential = header_secret.get_secret_value()
        if not credential.strip():
            credential = ""
            raise ProviderRequestError(
                "MISSING_CREDENTIAL",
                "Provider credential is unavailable",
            )
        return "header", "Authorization", credential
    if (
        type(query_secret) is not tuple
        or len(query_secret) != 2
        or type(query_secret[0]) is not str
        or not query_secret[0].strip()
        or query_secret[0] != query_secret[0].strip()
    ):
        raise TypeError("query_secret must be a (name, SecretStr) tuple")
    secret = query_secret[1]
    if secret is None:
        raise ProviderRequestError(
            "MISSING_CREDENTIAL",
            "Provider credential is unavailable",
        )
    if type(secret) is not SecretStr:
        raise TypeError("query credential must be an exact SecretStr or None")
    credential = secret.get_secret_value()
    if not credential.strip():
        credential = ""
        raise ProviderRequestError(
            "MISSING_CREDENTIAL",
            "Provider credential is unavailable",
        )
    return "query", query_secret[0], credential


def _validate_request_fields(
    *,
    url: str,
    params: Mapping[str, str],
    accepted_content_types: frozenset[str],
) -> None:
    if type(url) is not str or not url.startswith(("https://", "http://")):
        raise ValueError("provider URL must be absolute HTTP(S)")
    if type(params) is not dict or any(
        type(key) is not str or type(value) is not str for key, value in params.items()
    ):
        raise TypeError("provider params must be an exact string mapping")
    if type(accepted_content_types) is not frozenset or not accepted_content_types:
        raise TypeError("accepted content types must be a nonempty frozenset")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _schema_fingerprint(value: object) -> str:
    encoded = json.dumps(
        _schema_shape(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _schema_shape(value: object) -> object:
    if type(value) is dict:
        return {
            key: _schema_shape(item)
            for key, item in sorted(value.items())
            if type(key) is str
        }
    if type(value) is list:
        item_shapes = {
            json.dumps(
                _schema_shape(item),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in value
        }
        return {"arrayItemShapes": sorted(item_shapes)}
    if value is None:
        return "null"
    return type(value).__name__
