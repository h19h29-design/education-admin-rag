from math import sqrt
from pathlib import Path

from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.routing.models import Coordinate
from pyproj import Transformer
from shapely.geometry import Polygon

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


# Production break caught: using an inscribed polygon approximation that drops
# near-corner points whose exact projected distance is at most 12,000 m.
def test_coverage_uses_exact_projected_distance_at_buffer_corner() -> None:
    service = CoverageService(
        seoul_projected=Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        buffer_distance_m=12_000,
        wgs84_to_projected=Transformer.from_pipeline("+proj=noop"),
    )

    def point_at_distance(distance_m: float) -> Coordinate:
        offset = distance_m / sqrt(2)
        return Coordinate(latitude=100 + offset, longitude=100 + offset)

    assert service.classify(point_at_distance(11_999)) is CoverageState.BUFFER
    assert service.classify(point_at_distance(12_000)) is CoverageState.BUFFER
    assert service.classify(point_at_distance(12_000.001)) is CoverageState.OUTSIDE
