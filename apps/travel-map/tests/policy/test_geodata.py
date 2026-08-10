import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from shapely.geometry import Point, shape

FIXTURE_ROOT = Path("apps/travel-map/tests/fixtures/geodata")
SCRIPT = Path("apps/travel-map/scripts/build-geodata.py")
SOURCE_PAGE_URL = "https://www.data.go.kr/data/15059008/openapi.do"


# Production break caught: buffering longitude degrees instead of 12,000 projected meters.
def test_builder_normalizes_boundary_builds_buffer_and_records_hashes(
    tmp_path: Path,
) -> None:
    source = FIXTURE_ROOT / "seoul-square.geojson"
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
    assert not support_area.covers(Point(127.30, 37.55))
    assert manifest["source"] == {
        "pageUrl": SOURCE_PAGE_URL,
        "collectedAt": "2026-08-10T00:00:00Z",
        "crs": "OGC:CRS84",
        "sha256": sha256(source),
        "featureCount": 1,
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


# Production break caught: emitting self-intersecting production boundary geometry.
def test_builder_repairs_invalid_polygon(tmp_path: Path) -> None:
    output = tmp_path / "geodata"

    run_builder(FIXTURE_ROOT / "seoul-invalid.geojson", output)

    assert read_single_geometry(output / "seoul.geojson").is_valid
    assert read_single_geometry(output / "seoul-plus-12km.geojson").is_valid


# Production break caught: approving a source that does not contain Seoul City Hall.
def test_builder_rejects_non_seoul_source(tmp_path: Path) -> None:
    source = tmp_path / "not-seoul.geojson"
    source.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {},
                        "geometry": {
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
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        builder_command(source, tmp_path / "geodata"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source boundary does not contain Seoul City Hall" in completed.stderr


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
