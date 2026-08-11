from fastapi import APIRouter, HTTPException, Request

from app.api.common import client_ip, dependencies_for
from app.contracts import TripPreviewRequest, TripPreviewResponse
from app.institutions.store import UnknownSiteError
from app.services.trip_preview import TripPreviewService

router = APIRouter(tags=["trips"])


@router.post("/trips/preview", response_model=TripPreviewResponse)
async def trip_preview(
    request: Request,
    payload: TripPreviewRequest,
) -> TripPreviewResponse:
    dependencies = dependencies_for(request)
    decision = dependencies.rate_limiter.check("preview", client_ip(request))
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail="RATE_LIMITED",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
    try:
        return await TripPreviewService(dependencies).preview(payload)
    except UnknownSiteError:
        raise HTTPException(status_code=404, detail="UNKNOWN_ORIGIN_SITE") from None
