from fastapi import HTTPException, Request

from app.dependencies import AppDependencies


def dependencies_for(request: Request) -> AppDependencies:
    dependencies = getattr(request.app.state, "dependencies", None)
    if type(dependencies) is not AppDependencies:
        raise HTTPException(status_code=503, detail="SERVICE_UNAVAILABLE")
    return dependencies


def client_ip(request: Request) -> str:
    """Use only the socket client unless trusted-proxy support is explicitly added."""

    return request.client.host if request.client is not None else "unknown"
