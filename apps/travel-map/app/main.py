import logging
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import router as api_router
from app.dependencies import AppDependencies, build_production_dependencies
from app.settings import Settings

_ACCESS_LOG = logging.getLogger("travel_map.access")
_MAX_REQUEST_BYTES = 32 * 1024
_MAX_CONTENT_LENGTH_DIGITS = 20
# Kakao Maps loads the SDK bootstrap/API from dapi.kakao.com, its runtime from
# t1.daumcdn.net, and map tile images from Daum CDN subdomains. These are the
# only third-party origins required by the public map; no inline code is allowed.
_PUBLIC_CONTENT_SECURITY_POLICY = (
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


class RequestTooLargeError(Exception):
    pass


class JsonTrustedHostMiddleware(TrustedHostMiddleware):
    """Keep exact host validation while returning the API's JSON error contract."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        host = Headers(scope=scope).get("host", "").split(":")[0]
        if any(
            host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:]))
            for pattern in self.allowed_hosts
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            await _json_error(scope, receive, send, 400, "INVALID_HOST")
            return
        await send({"type": "websocket.close", "code": 1008})


class _UvicornQueryRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if type(record.args) is tuple and len(record.args) >= 3:
            request_target = record.args[2]
            if type(request_target) is str:
                record.args = (
                    *record.args[:2],
                    request_target.split("?", maxsplit=1)[0],
                    *record.args[3:],
                )
        return True


def _configure_uvicorn_access_log() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(type(item) is _UvicornQueryRedactionFilter for item in logger.filters):
        logger.addFilter(_UvicornQueryRedactionFilter())


class RequestSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            content_length = _content_length(scope)
        except RequestTooLargeError:
            await _json_error(scope, receive, send, 400, "INVALID_CONTENT_LENGTH")
            return
        if content_length is not None and content_length > self.max_bytes:
            await _json_error(scope, receive, send, 413, "REQUEST_TOO_LARGE")
            return
        try:
            events = await _buffer_request_events(receive, self.max_bytes)
        except RequestTooLargeError:
            await _json_error(scope, receive, send, 413, "REQUEST_TOO_LARGE")
            return

        async def replay_receive() -> Message:
            if events:
                return events.popleft()
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def _content_length(scope: Scope) -> int | None:
    values = [
        value for name, value in scope["headers"] if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if (
        len(values) != 1
        or not values[0].isdigit()
        or len(values[0]) > _MAX_CONTENT_LENGTH_DIGITS
    ):
        raise RequestTooLargeError
    try:
        return int(values[0])
    except ValueError:
        raise RequestTooLargeError from None


async def _buffer_request_events(
    receive: Receive,
    max_bytes: int,
) -> deque[Message]:
    events: deque[Message] = deque()
    received = 0
    while True:
        event = await receive()
        events.append(event)
        if event["type"] == "http.disconnect":
            return events
        if event["type"] != "http.request":
            raise RequestTooLargeError
        body = event.get("body", b"")
        if not isinstance(body, bytes):
            raise RequestTooLargeError
        received += len(body)
        if received > max_bytes:
            raise RequestTooLargeError
        if not event.get("more_body", False):
            return events


async def _json_error(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    code: str,
) -> None:
    response = JSONResponse({"error": {"code": code}}, status_code=status_code)
    await response(scope, receive, send)


def create_app(
    settings: Settings | None = None,
    dependencies: AppDependencies | None = None,
) -> FastAPI:
    active_settings = settings or Settings()
    _configure_uvicorn_access_log()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_dependencies = dependencies
        if active_dependencies is None and active_settings.environment == "production":
            active_dependencies = build_production_dependencies(active_settings)
        app.state.dependencies = active_dependencies
        try:
            yield
        finally:
            if active_dependencies is not None:
                await active_dependencies.aclose()

    app = FastAPI(
        title="서울교육기관 관내출장 지도",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=_MAX_REQUEST_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    allowed_hosts = list(active_settings.allowed_hosts)
    if active_settings.environment != "production":
        allowed_hosts.append("testserver")
    app.add_middleware(
        JsonTrustedHostMiddleware,
        allowed_hosts=allowed_hosts,
        www_redirect=False,
    )

    @app.middleware("http")
    async def safe_access_log(request: Request, call_next: object) -> object:
        started = perf_counter()
        response = await call_next(request)  # type: ignore[operator]
        _ACCESS_LOG.info(
            "path=%s status=%s latency_ms=%d",
            request.url.path,
            response.status_code,
            int((perf_counter() - started) * 1000),
        )
        return response

    @app.middleware("http")
    async def content_security_policy(request: Request, call_next: object) -> object:
        response = await call_next(request)  # type: ignore[operator]
        response.headers.setdefault("Content-Security-Policy", _PUBLIC_CONTENT_SECURITY_POLICY)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "VALIDATION_ERROR"}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else "REQUEST_FAILED"
        return JSONResponse(
            {"error": {"code": detail}},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(api_router)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).with_name("static"), check_dir=False),
        name="static",
    )
    app.mount(
        "/",
        StaticFiles(
            directory=Path(__file__).with_name("static"),
            html=True,
            check_dir=False,
        ),
        name="travel-map-ui",
    )
    return app


app = create_app()
