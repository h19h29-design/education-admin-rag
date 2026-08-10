import hashlib
import json
from pathlib import Path

from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.routing.models import Coordinate
from shapely.geometry import Point, shape

GEODATA_ROOT = Path("apps/travel-map/resources/geodata")
SOURCE_PATH = GEODATA_ROOT / "source/seoul-boundary.geojson"
SEOUL_PATH = GEODATA_ROOT / "seoul.geojson"
SUPPORT_PATH = GEODATA_ROOT / "seoul-plus-12km.geojson"
MANIFEST_PATH = GEODATA_ROOT / "manifest.json"
SEOUL_CITY_HALL = Coordinate(37.5665, 126.9780)
INCHEON_CITY_HALL = Coordinate(37.4563, 126.7052)
SUWON_CITY_HALL = Coordinate(37.2629820, 127.0284632)
ARCHIVE_SHA256 = "f1cf0f9de453ac7eaacb273f39cee52851183372b9ddfda428a967c3a670b2c6"


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
    assert manifest["coverage"] == {
        "purpose": "MAP_SUPPORT_AREA_ONLY",
        "bufferDistanceMeters": 12_000,
        "legalClassificationBasis": "NETWORK_ROUND_TRIP_DISTANCE",
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
