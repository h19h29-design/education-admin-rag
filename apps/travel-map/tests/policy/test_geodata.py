import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from shapely.geometry import Point, shape

FIXTURE_ROOT = Path("apps/travel-map/tests/fixtures/geodata")
PRODUCTION_SOURCE = Path(
    "apps/travel-map/resources/geodata/source/seoul-boundary.geojson"
)
SCRIPT = Path("apps/travel-map/scripts/build-geodata.py")
SGIS_PAGE_URL = "https://www.data.go.kr/data/15129688/fileData.do"
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


# Production break caught: buffering longitude degrees instead of 12,000 projected meters.
def test_builder_normalizes_boundary_builds_buffer_and_records_hashes(
    tmp_path: Path,
) -> None:
    source = PRODUCTION_SOURCE
    output = tmp_path / "geodata"

    run_builder(source, output)

    seoul_path = output / "seoul.geojson"
    buffer_path = output / "seoul-plus-12km.geojson"
    manifest_path = output / "manifest.json"
    seoul = read_single_geometry(seoul_path)
    support_area = read_single_geometry(buffer_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert seoul.covers(Point(126.98, 37.55))
    assert support_area.covers(Point(127.09, 37.55))
    assert not support_area.covers(Point(127.0284632, 37.2629820))
    assert manifest["generatedAt"] == "2026-08-10T00:00:00Z"
    assert manifest["coverage"] == EXPECTED_COVERAGE_METADATA
    assert manifest["sourceArchive"] == {
        "pageUrl": SGIS_PAGE_URL,
        "datasetName": "국가데이터처_SGIS 행정구역 통계 및 경계_20250630",
        "referencePeriod": "2025 Q2",
        "fileIdentifier": "FILE_000000003681593",
        "detailNumber": 1,
        "sha256": ARCHIVE_SHA256,
        "layer": "bnd_sido_00_2025_2Q",
        "crs": {
            "authority": "ESRI:102080",
            "name": "Korea_2000_Korea_Unified_Coordinate_System",
        },
        "featureCount": 17,
        "collectedAt": "2026-08-10T08:10:45Z",
    }
    assert manifest["source"] == {
        "crs": "OGC:CRS84",
        "sha256": sha256(source),
        "featureCount": 1,
        "baseDate": "20250630",
        "administrativeCode": "11",
        "administrativeName": "서울특별시",
    }
    assert manifest["outputs"]["seoul.geojson"] == {
        "crs": "OGC:CRS84",
        "sha256": sha256(seoul_path),
        "featureCount": 1,
    }
    assert manifest["outputs"]["seoul-plus-12km.geojson"] == {
        "crs": "OGC:CRS84",
        "sha256": sha256(buffer_path),
        "featureCount": 1,
    }


# Production break caught: serializing projection noise beyond useful WGS84 precision.
def test_builder_limits_output_coordinates_to_seven_decimal_places(
    tmp_path: Path,
) -> None:
    output = tmp_path / "geodata"
    run_builder(PRODUCTION_SOURCE, output)

    for filename in ("seoul.geojson", "seoul-plus-12km.geojson"):
        payload = json.loads((output / filename).read_text(encoding="utf-8"))
        coordinates = payload["features"][0]["geometry"]["coordinates"]
        assert all(value == round(value, 7) for value in coordinate_values(coordinates))


# Production break caught: emitting self-intersecting production boundary geometry.
def test_builder_repairs_invalid_polygon() -> None:
    payload = json.loads(
        (FIXTURE_ROOT / "seoul-invalid.geojson").read_text(encoding="utf-8")
    )

    prepared = load_builder_module().prepare_geodata(payload)

    assert prepared.seoul_wgs84.is_valid
    assert prepared.support_wgs84.is_valid


# Production break caught: approving a source that does not contain Seoul City Hall.
def test_builder_rejects_non_seoul_source() -> None:
    fixture_payload = json.loads(
        (FIXTURE_ROOT / "seoul-square.geojson").read_text(encoding="utf-8")
    )
    fixture_payload["features"][0]["geometry"] = {
        "type": "Polygon",
        "coordinates": [
            [
                [128.0, 38.0],
                [128.1, 38.0],
                [128.1, 38.1],
                [128.0, 38.1],
                [128.0, 38.0],
            ]
        ],
    }

    with pytest.raises(
        ValueError, match="source boundary does not contain Seoul City Hall"
    ):
        load_builder_module().prepare_geodata(fixture_payload)


# Production break caught: silently replacing missing or malformed acquisition
# metadata with the current clock time, or leaking an unhandled traceback.
@pytest.mark.parametrize("collected_at", [None, 123, "2026-08-10T08:00:00"])
def test_builder_rejects_missing_or_invalid_source_collection_time(
    tmp_path: Path,
    collected_at: object,
) -> None:
    source = tmp_path / "source.geojson"
    payload = json.loads(PRODUCTION_SOURCE.read_text(encoding="utf-8"))
    if collected_at is None:
        payload["_provenance"].pop("collectedAt")
    else:
        payload["_provenance"]["collectedAt"] = collected_at
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "geodata"

    completed = subprocess.run(
        builder_command(source, output),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source provenance collectedAt" in completed.stderr
    assert "Traceback" not in completed.stderr
    assert not output.exists()


# Production break caught: recording a valid offset timestamp inconsistently
# instead of preserving one canonical UTC provenance instant.
def test_builder_normalizes_source_collection_time_to_utc(tmp_path: Path) -> None:
    source = tmp_path / "source.geojson"
    payload = json.loads(PRODUCTION_SOURCE.read_text(encoding="utf-8"))
    payload["_provenance"]["collectedAt"] = "2026-08-10T09:00:00+09:00"
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "geodata"

    run_builder(source, output)

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sourceArchive"]["collectedAt"] == "2026-08-10T00:00:00Z"


# Production break caught: accepting a well-formed but false archive digest as
# official SGIS provenance during manifest generation.
def test_builder_rejects_unpinned_official_archive_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.geojson"
    payload = json.loads(PRODUCTION_SOURCE.read_text(encoding="utf-8"))
    payload["_provenance"]["archiveSha256"] = "0" * 64
    source.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "geodata"

    completed = subprocess.run(
        builder_command(source, output),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "unexpected source provenance archiveSha256" in completed.stderr
    assert not output.exists()


def run_builder(source: Path, output: Path) -> None:
    subprocess.run(
        builder_command(source, output),
        check=True,
        capture_output=True,
        text=True,
    )


def builder_command(source: Path, output: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--source",
        str(source),
        "--output",
        str(output),
        "--collected-at",
        "2026-08-10T00:00:00Z",
    ]


def read_single_geometry(path: Path) -> Any:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["features"]) == 1
    return shape(payload["features"][0]["geometry"])


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def coordinate_values(value: Any) -> list[float]:
    if isinstance(value, (float, int)):
        return [float(value)]
    values: list[float] = []
    for child in value:
        values.extend(coordinate_values(child))
    return values


def load_builder_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_geodata", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
