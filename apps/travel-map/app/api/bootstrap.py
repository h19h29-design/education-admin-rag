from fastapi import APIRouter, Request

from app.api.common import dependencies_for

router = APIRouter(tags=["bootstrap"])


@router.get("/bootstrap")
async def bootstrap(request: Request) -> dict[str, object]:
    dependencies = dependencies_for(request)
    javascript_key = dependencies.settings.kakao_javascript_key
    return {
        "map": {
            "javascriptKey": (
                javascript_key.get_secret_value()
                if javascript_key is not None
                else None
            )
        }
    }
