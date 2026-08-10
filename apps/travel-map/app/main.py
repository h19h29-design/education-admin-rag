from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="서울교육기관 관내출장 지도", version="0.1.0")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
