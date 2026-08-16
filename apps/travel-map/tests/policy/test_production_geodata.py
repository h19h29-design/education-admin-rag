import hashlib
import json
from pathlib import Path

from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.routing.models import Coordinate
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import transform

GEODATA_ROOT = Path("apps/travel-map/resources/geodata")
SOURCE_PATH = GEODATA_ROOT / "source/seoul-boundary.geojson"
SEOUL_PATH = GEODATA_ROOT / "seoul.geojson"
SUPPORT_PATH = GEODATA_ROOT / "seoul-plus-12km.geojson"
MANIFEST_PATH = GEODATA_ROOT / "manifest.json"
SEOUL_CITY_HALL = Coordinate(37.5665, 126.9780)
INCHEON_CITY_HALL = Coordinate(37.4563, 126.7052)
SUWON_CITY_HALL = Coordinate(37.2629820, 127.0284632)
ARCHIVE_SHA256 = "f1cf0f9de453ac7eaacb273f39cee52851183372b9ddfda428a967c3a670b2c6"
EXPECTED_COVERAGE_METADATA = {
    "purpose": "MAP_SUPPORT_AREA_ONLY",
    "bufferDistanceMeters": 12_000,
    "legalClassificationBasis": "NETWORK_ROUND_TRIP_DISTANCE",
    "runtimeClassification": "EXACT_PROJECTED_DISTANCE_LTE_BUFFER",
    "providerPolygonApproximation": {
        "method": "CONSERVATIVE_PROJECTED_ROUND_BUFFER",
        "projectedCrs": "EPSG:5179",
        "quadSegments": 64,
        "nominalBufferDistanceMeters": 12_000,
        "generatedBufferDistanceMeters": 12_002,
        "conservativePaddingMeters": 2,
        "maximumChordErrorMeters": 0.904,
        "maximumCoordinateRoundingErrorMeters": 0.02,
        "minimumCoverageMarginMeters": 1.076,
        "maximumOvercoverageMeters": 2.02,
    },
}


# Production break caught: treating the factual 9.889 km Incheon point as outside.
def test_production_coverage_classifies_three_factual_sentinels() -> None:
    service = CoverageService.from_geojson(
        seoul_path=SEOUL_PATH,
        buffer_distance_m=12_000,
    )
    support_payload = json.loads(SUPPORT_PATH.read_text(encoding="utf-8"))
    support_geometry = shape(support_payload["features"][0]["geometry"])

    assert service.classify(SEOUL_CITY_HALL) is CoverageState.SEOUL
    assert service.classify(INCHEON_CITY_HALL) is CoverageState.BUFFER
    assert service.classify(SUWON_CITY_HALL) is CoverageState.OUTSIDE
    assert support_geometry.covers(
        Point(INCHEON_CITY_HALL.longitude, INCHEON_CITY_HALL.latitude)
    )
    assert not support_geometry.covers(
        Point(SUWON_CITY_HALL.longitude, SUWON_CITY_HALL.latitude)
    )


# Production break caught: recording provenance hashes that differ from shipped bytes.
def test_production_manifest_hashes_and_scope_match_shipped_artifacts() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["sourceArchive"]["sha256"] == ARCHIVE_SHA256
    assert manifest["source"]["sha256"] == sha256(SOURCE_PATH)
    assert manifest["outputs"]["seoul.geojson"]["sha256"] == sha256(SEOUL_PATH)
    assert manifest["outputs"]["seoul-plus-12km.geojson"]["sha256"] == sha256(
        SUPPORT_PATH
    )
    assert manifest["coverage"] == EXPECTED_COVERAGE_METADATA


# Production break caught: shipping an inscribed provider polygon that omits
# destinations within the nominal 12 km support distance near convex corners.
def test_production_provider_polygon_conservatively_covers_nominal_buffer() -> None:
    to_projected = Transformer.from_crs(
        "OGC:CRS84", "EPSG:5179", always_xy=True
    )
    seoul_payload = json.loads(SEOUL_PATH.read_text(encoding="utf-8"))
    support_payload = json.loads(SUPPORT_PATH.read_text(encoding="utf-8"))
    seoul_projected = transform(
        to_projected.transform,
        shape(seoul_payload["features"][0]["geometry"]),
    )
    provider_projected = transform(
        to_projected.transform,
        shape(support_payload["features"][0]["geometry"]),
    )
    nominal_reference = seoul_projected.buffer(12_000, quad_segs=256)

    assert provider_projected.covers(nominal_reference)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
