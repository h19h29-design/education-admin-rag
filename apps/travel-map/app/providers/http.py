import asyncio
import json
from collections.abc import Mapping
from math import isfinite
from typing import Any

import httpx
from pydantic import SecretStr

from app.routing.models import ProviderWarning


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
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.owns_http:
            await self.http.aclose()

    async def get_json(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        header_secret: SecretStr | None = None,
        query_secret: tuple[str, SecretStr | None] | None = None,
    ) -> dict[str, Any]:
        raw = await self._get_bytes(
            url=url,
            params=params,
            accepted_content_types=frozenset({"application/json"}),
            header_secret=header_secret,
            query_secret=query_secret,
        )
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
        return value

    async def get_xml(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        query_secret: tuple[str, SecretStr | None],
    ) -> bytes:
        return await self._get_bytes(
            url=url,
            params=params,
            accepted_content_types=frozenset(
                {"application/xml", "text/xml", "application/xhtml+xml"}
            ),
            query_secret=query_secret,
        )

    async def _get_bytes(
        self,
        *,
        url: str,
        params: Mapping[str, str],
        accepted_content_types: frozenset[str],
        header_secret: SecretStr | None = None,
        query_secret: tuple[str, SecretStr | None] | None = None,
    ) -> bytes:
        if type(url) is not str or not url.startswith(("https://", "http://")):
            raise ValueError("provider URL must be absolute HTTP(S)")
        if type(params) is not dict or any(
            type(key) is not str or type(value) is not str
            for key, value in params.items()
        ):
            raise TypeError("provider params must be an exact string mapping")
        if type(accepted_content_types) is not frozenset or not accepted_content_types:
            raise TypeError("accepted content types must be a nonempty frozenset")
        if header_secret is None and query_secret is None:
            raise ProviderRequestError(
                "MISSING_CREDENTIAL",
                "Provider credential is unavailable",
            )
        if header_secret is not None and type(header_secret) is not SecretStr:
            raise TypeError("header_secret must be an exact SecretStr or None")
        if query_secret is not None:
            if (
                type(query_secret) is not tuple
                or len(query_secret) != 2
                or type(query_secret[0]) is not str
            ):
                raise TypeError("query_secret must be a (name, SecretStr) tuple")
            if query_secret[1] is not None and type(query_secret[1]) is not SecretStr:
                raise TypeError("query credential must be an exact SecretStr or None")

        params_buffer = dict(params)
        headers: dict[str, str] = {}
        credential = ""
        body = bytearray()
        failure: tuple[str, str] | None = None
        try:
            if header_secret is not None:
                credential = header_secret.get_secret_value()
                if not credential.strip():
                    failure = (
                        "MISSING_CREDENTIAL",
                        "Provider credential is unavailable",
                    )
                else:
                    headers["Authorization"] = f"KakaoAK {credential}"
            elif query_secret is not None:
                secret = query_secret[1]
                if secret is None:
                    failure = (
                        "MISSING_CREDENTIAL",
                        "Provider credential is unavailable",
                    )
                else:
                    credential = secret.get_secret_value()
                    if not credential.strip():
                        failure = (
                            "MISSING_CREDENTIAL",
                            "Provider credential is unavailable",
                        )
                    else:
                        params_buffer[query_secret[0]] = credential

            if failure is None:
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


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")
