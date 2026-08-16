"""Versioned public API routers."""

from fastapi import APIRouter

from app.api.bootstrap import router as bootstrap_router
from app.api.geodata import router as geodata_router
from app.api.institutions import router as institutions_router
from app.api.places import router as places_router
from app.api.trips import router as trips_router

router = APIRouter(prefix="/api/v1")
router.include_router(bootstrap_router)
router.include_router(institutions_router)
router.include_router(geodata_router)
router.include_router(places_router)
router.include_router(trips_router)
