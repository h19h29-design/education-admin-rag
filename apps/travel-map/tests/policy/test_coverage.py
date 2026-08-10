from pathlib import Path

from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.routing.models import Coordinate

FIXTURE = Path("apps/travel-map/tests/fixtures/geodata/seoul-square.geojson")


# Production break caught: treating the 12 km support buffer as Seoul or omitting it.
def test_coverage_separates_seoul_buffer_and_outside() -> None:
    service = CoverageService.from_geojson(
        seoul_path=FIXTURE,
        buffer_distance_m=12_000,
    )

    assert service.classify(Coordinate(37.55, 126.98)) is CoverageState.SEOUL
    assert service.classify(Coordinate(37.55, 127.09)) is CoverageState.BUFFER
    assert service.classify(Coordinate(37.55, 127.30)) is CoverageState.OUTSIDE


# Production break caught: excluding a destination exactly on the Seoul boundary.
def test_coverage_includes_polygon_boundary_in_seoul() -> None:
    service = CoverageService.from_geojson(
        seoul_path=FIXTURE,
        buffer_distance_m=12_000,
    )

    assert service.classify(Coordinate(37.55, 127.00)) is CoverageState.SEOUL
