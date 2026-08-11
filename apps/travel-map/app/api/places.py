from typing import Annotated, cast

from fastapi import APIRouter, HTTPException, Query, Request

from app.api.common import client_ip, dependencies_for
from app.cache import PLACES_TTL_SECONDS
from app.contracts import PlacesResponse, ReversePlaceResponse
from app.providers.kakao_local import BoundingBox, PlaceCandidate
from app.routing.models import Coordinate

router = APIRouter(tags=["places"])
_SEOUL_BOUNDS = BoundingBox(124.0, 33.0, 132.0, 39.5)


@router.get("/places", response_model=PlacesResponse)
async def places(
    request: Request,
    q: Annotated[str, Query(min_length=2, max_length=80)],
) -> PlacesResponse:
    dependencies = dependencies_for(request)
    _check_places_limit(request)
    key = dependencies.cache.key("places", {"query": q.strip()})
    cached = dependencies.cache.get(key)
    if isinstance(cached, tuple):
        candidates = cast(tuple[PlaceCandidate, ...], cached)
        return PlacesResponse(items=tuple(_place_response(item) for item in candidates))
    candidates = await dependencies.place_client.search(q, bounds=_SEOUL_BOUNDS)  # type: ignore[attr-defined]
    warnings = tuple(getattr(dependencies.place_client, "last_warnings", ()))
    if not candidates and warnings:
        raise HTTPException(status_code=503, detail="PLACE_PROVIDER_UNAVAILABLE")
    dependencies.cache.set(key, candidates, ttl_seconds=PLACES_TTL_SECONDS)
    return PlacesResponse(
        items=tuple(_place_response(item) for item in candidates),
        warnings=warnings,
    )


@router.get("/places/reverse", response_model=ReversePlaceResponse)
async def reverse_places(
    request: Request,
    latitude: Annotated[float, Query(ge=33.0, le=39.5)],
    longitude: Annotated[float, Query(ge=124.0, le=132.0)],
) -> ReversePlaceResponse:
    dependencies = dependencies_for(request)
    _check_places_limit(request)
    coordinate = Coordinate(latitude=latitude, longitude=longitude)
    key = dependencies.cache.key(
        "places-reverse",
        {"latitude": latitude, "longitude": longitude},
    )
    cached = dependencies.cache.get(key)
    if type(cached) is ReversePlaceResponse:
        return cached
    candidate = await dependencies.place_client.reverse_geocode(coordinate)  # type: ignore[attr-defined]
    warnings = tuple(getattr(dependencies.place_client, "last_warnings", ()))
    if candidate is None and warnings:
        raise HTTPException(status_code=503, detail="PLACE_PROVIDER_UNAVAILABLE")
    response = ReversePlaceResponse(
        item=_place_response(candidate) if candidate is not None else None,
        warnings=warnings,
    )
    return dependencies.cache.set(
        key,
        response,
        ttl_seconds=PLACES_TTL_SECONDS,
    )


def _check_places_limit(request: Request) -> None:
    decision = dependencies_for(request).rate_limiter.check(
        "places", client_ip(request)
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="RATE_LIMITED",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _place_response(item: PlaceCandidate) -> dict[str, object]:
    return {
        "placeId": item.place_id,
        "name": item.name,
        "roadAddress": item.road_address,
        "lotAddress": item.lot_address,
        "latitude": item.latitude,
        "longitude": item.longitude,
    }
