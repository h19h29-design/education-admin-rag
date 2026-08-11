import hashlib

from fastapi import APIRouter, Request, Response

from app.api.common import dependencies_for

router = APIRouter(tags=["geodata"])
_CACHE_CONTROL = "public, max-age=86400, immutable"


@router.get("/geodata/seoul")
async def seoul_geodata(request: Request) -> Response:
    return _geojson_response(request, "seoul_geojson")


@router.get("/geodata/support")
async def support_geodata(request: Request) -> Response:
    return _geojson_response(request, "support_geojson")


def _geojson_response(request: Request, attribute: str) -> Response:
    payload = getattr(dependencies_for(request), attribute)
    etag = f'"{hashlib.sha256(payload).hexdigest()}"'
    headers = {"ETag": etag, "Cache-Control": _CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=payload,
        media_type="application/geo+json",
        headers=headers,
    )
