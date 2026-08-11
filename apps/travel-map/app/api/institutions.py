from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.api.common import dependencies_for
from app.contracts import InstitutionSearchResponse

router = APIRouter(tags=["institutions"])


@router.get("/institutions", response_model=InstitutionSearchResponse)
async def institutions(
    request: Request,
    q: Annotated[str, Query(max_length=120)] = "",
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    institution_type: Annotated[str | None, Query(max_length=80)] = None,
    foundation_type: Annotated[str | None, Query(max_length=80)] = None,
    education_office: Annotated[str | None, Query(max_length=120)] = None,
    district: Annotated[str | None, Query(max_length=80)] = None,
) -> InstitutionSearchResponse:
    dependencies = dependencies_for(request)
    items = dependencies.institutions.search(
        query=q,
        institution_type=institution_type,
        foundation_type=foundation_type,
        education_office=education_office,
        district=district,
        limit=limit,
    )
    return InstitutionSearchResponse(
        items=tuple(item.model_dump(by_alias=True) for item in items)
    )
