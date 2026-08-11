import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import router as api_router
from app.dependencies import AppDependencies, build_production_dependencies
from app.settings import Settings

_ACCESS_LOG = logging.getLogger("travel_map.access")
_MAX_REQUEST_BYTES = 32 * 1024


class RequestTooLargeError(Exception):
    pass


class RequestSizeLimitMiddleware:
    def __init__(self, app: object, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        if not isinstance(scope, dict) or scope.get("type") != "http":
            await self.app(scope, receive, send)  # type: ignore[operator]
            return
        headers = dict(scope.get("headers", ()))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await _json_error(send, 413, "REQUEST_TOO_LARGE")
                    return
            except ValueError:
                await _json_error(send, 400, "INVALID_CONTENT_LENGTH")
                return
        received = 0

        async def limited_receive() -> object:
            nonlocal received
            event = await receive()  # type: ignore[operator]
            if isinstance(event, dict) and event.get("type") == "http.request":
                body = event.get("body", b"")
                if isinstance(body, bytes):
                    received += len(body)
                    if received > self.max_bytes:
                        raise RequestTooLargeError
            return event

        try:
            await self.app(scope, limited_receive, send)  # type: ignore[operator]
        except RequestTooLargeError:
            await _json_error(send, 413, "REQUEST_TOO_LARGE")


async def _json_error(send: object, status_code: int, code: str) -> None:
    response = JSONResponse({"error": {"code": code}}, status_code=status_code)
    await response(None, None, send)  # type: ignore[arg-type]


def create_app(
    settings: Settings | None = None,
    dependencies: AppDependencies | None = None,
) -> FastAPI:
    active_settings = settings or Settings()

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
    allowed_hosts = list(active_settings.allowed_hosts)
    if active_settings.environment != "production":
        allowed_hosts.append("testserver")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
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
    return app


app = create_app()
