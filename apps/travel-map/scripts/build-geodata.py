#!/usr/bin/env python3
import argparse
import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pyproj import CRS, Transformer
from shapely import make_valid  # type: ignore[import-untyped]
from shapely.geometry import (  # type: ignore[import-untyped]
    GeometryCollection,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
)
from shapely.geometry import shape as shape_geometry  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import transform, unary_union  # type: ignore[import-untyped]

SOURCE_PAGE_URL = "https://www.data.go.kr/data/15059008/openapi.do"
OUTPUT_CRS = "OGC:CRS84"
PROJECTED_CRS = "EPSG:5179"
BUFFER_DISTANCE_M = 12_000
SEOUL_CITY_HALL = Point(126.9780, 37.5665)
INCHEON_CITY_HALL = Point(126.7052, 37.4563)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build normalized Seoul and 12 km support-area GeoJSON."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--collected-at",
        help="UTC ISO-8601 collection time; defaults to the current time.",
    )
    args = parser.parse_args()

    try:
        build_geodata(
            source_path=args.source,
            output_directory=args.output,
            collected_at=normalize_collected_at(args.collected_at),
        )
    except (KeyError, TypeError, ValueError) as error:
        parser.error(str(error))


def build_geodata(
    *,
    source_path: Path,
    output_directory: Path,
    collected_at: str,
) -> None:
    source_payload = cast(
        dict[str, Any], json.loads(source_path.read_text(encoding="utf-8"))
    )
    if source_payload.get("type") != "FeatureCollection":
        raise ValueError("source must be a GeoJSON FeatureCollection")
    features = source_payload.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("source FeatureCollection must contain at least one feature")

    source_crs, source_crs_label = read_source_crs(source_payload)
    source_geometries = [
        shape_geometry(cast(dict[str, Any], feature)["geometry"])
        for feature in features
    ]
    source_boundary = polygonal_geometry(unary_union(source_geometries))
    to_wgs84 = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
    seoul_wgs84 = polygonal_geometry(transform(to_wgs84.transform, source_boundary))
    if not seoul_wgs84.covers(SEOUL_CITY_HALL):
        raise ValueError("source boundary does not contain Seoul City Hall")

    to_projected = Transformer.from_crs("EPSG:4326", PROJECTED_CRS, always_xy=True)
    to_output = Transformer.from_crs(PROJECTED_CRS, "EPSG:4326", always_xy=True)
    seoul_projected = polygonal_geometry(transform(to_projected.transform, seoul_wgs84))
    support_projected = polygonal_geometry(seoul_projected.buffer(BUFFER_DISTANCE_M))
    support_wgs84 = polygonal_geometry(
        transform(to_output.transform, support_projected)
    )
    if support_wgs84.covers(INCHEON_CITY_HALL):
        raise ValueError("12 km support area unexpectedly contains Incheon City Hall")

    output_directory.mkdir(parents=True, exist_ok=True)
    seoul_path = output_directory / "seoul.geojson"
    support_path = output_directory / "seoul-plus-12km.geojson"
    write_geojson(
        seoul_path,
        seoul_wgs84,
        properties={"name": "서울특별시", "sourcePageUrl": SOURCE_PAGE_URL},
    )
    write_geojson(
        support_path,
        support_wgs84,
        properties={
            "name": "서울특별시 12km 지원영역",
            "bufferDistanceMeters": BUFFER_DISTANCE_M,
            "sourcePageUrl": SOURCE_PAGE_URL,
        },
    )

    manifest = {
        "source": {
            "pageUrl": SOURCE_PAGE_URL,
            "collectedAt": collected_at,
            "crs": source_crs_label,
            "sha256": sha256(source_path),
            "featureCount": len(features),
        },
        "outputs": {
            seoul_path.name: {
                "crs": OUTPUT_CRS,
                "sha256": sha256(seoul_path),
                "featureCount": 1,
            },
            support_path.name: {
                "crs": OUTPUT_CRS,
                "sha256": sha256(support_path),
                "featureCount": 1,
            },
        },
    }
    write_json(output_directory / "manifest.json", manifest)


def read_source_crs(payload: dict[str, Any]) -> tuple[CRS, str]:
    crs_payload = payload.get("crs")
    if crs_payload is None:
        return CRS.from_user_input("OGC:CRS84"), OUTPUT_CRS
    if not isinstance(crs_payload, dict):
        raise TypeError("source GeoJSON crs must be an object")
    properties = crs_payload.get("properties")
    if not isinstance(properties, dict) or not isinstance(properties.get("name"), str):
        raise TypeError("source GeoJSON crs.properties.name is required")
    name = properties["name"]
    crs = CRS.from_user_input(name)
    if crs == CRS.from_user_input("OGC:CRS84") or crs == CRS.from_epsg(4326):
        return crs, OUTPUT_CRS
    authority = crs.to_authority()
    label = f"{authority[0]}:{authority[1]}" if authority else crs.to_string()
    return crs, label


def polygonal_geometry(geometry: BaseGeometry) -> BaseGeometry:
    repaired = make_valid(geometry)
    polygon_parts = tuple(iter_polygons(repaired))
    if not polygon_parts:
        raise ValueError("source does not contain polygonal geometry")
    polygonal = unary_union(polygon_parts)
    if polygonal.is_empty or not polygonal.is_valid:
        raise ValueError("could not produce valid polygonal geometry")
    return polygonal


def iter_polygons(geometry: BaseGeometry) -> Iterable[Polygon]:
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from iter_polygons(part)


def write_geojson(
    path: Path,
    geometry: BaseGeometry,
    *,
    properties: dict[str, Any],
) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": properties,
                "geometry": mapping(geometry),
            }
        ],
    }
    write_json(path, payload)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_collected_at(value: str | None) -> str:
    if value is None:
        value = datetime.now(UTC).replace(microsecond=0).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("collected-at must include a timezone")
    return (
        parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


if __name__ == "__main__":
    main()
