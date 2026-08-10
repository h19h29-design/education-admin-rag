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

SOURCE_PAGE_URL = "https://www.data.go.kr/data/15129688/fileData.do"
DATASET_NAME = "국가데이터처_SGIS 행정구역 통계 및 경계_20250630"
REFERENCE_PERIOD = "2025 Q2"
FILE_IDENTIFIER = "FILE_000000003681593"
SOURCE_LAYER = "bnd_sido_00_2025_2Q"
SOURCE_LAYER_CRS = {
    "authority": "ESRI:102080",
    "name": "Korea_2000_Korea_Unified_Coordinate_System",
}
OUTPUT_CRS = "OGC:CRS84"
PROJECTED_CRS = "EPSG:5179"
BUFFER_DISTANCE_M = 12_000
COORDINATE_PRECISION = 7
SEOUL_CITY_HALL = Point(126.9780, 37.5665)
SUWON_CITY_HALL = Point(127.0284632, 37.2629820)


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
    if len(features) != 1:
        raise ValueError("source FeatureCollection must contain exactly one feature")
    provenance = read_provenance(source_payload)
    feature = cast(dict[str, Any], features[0])
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        raise TypeError("source feature properties must be an object")
    expected_properties = {
        "BASE_DATE": "20250630",
        "SIDO_CD": "11",
        "SIDO_NM": "서울특별시",
    }
    if any(properties.get(key) != value for key, value in expected_properties.items()):
        raise ValueError("source feature is not the 2025 Q2 Seoul boundary")

    source_crs, source_crs_label = read_source_crs(source_payload)
    source_geometries = [shape_geometry(feature["geometry"])]
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
    if support_wgs84.covers(SUWON_CITY_HALL):
        raise ValueError("12 km support area unexpectedly contains Suwon City Hall")

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
        "generatedAt": collected_at,
        "coverage": {
            "purpose": "MAP_SUPPORT_AREA_ONLY",
            "bufferDistanceMeters": BUFFER_DISTANCE_M,
            "legalClassificationBasis": "NETWORK_ROUND_TRIP_DISTANCE",
        },
        "sourceArchive": {
            "pageUrl": provenance["pageUrl"],
            "datasetName": provenance["datasetName"],
            "referencePeriod": provenance["referencePeriod"],
            "fileIdentifier": provenance["fileIdentifier"],
            "detailNumber": provenance["detailNumber"],
            "sha256": provenance["archiveSha256"],
            "layer": provenance["sourceLayer"],
            "crs": provenance["sourceLayerCrs"],
            "featureCount": provenance["sourceLayerFeatureCount"],
            "collectedAt": provenance["collectedAt"],
        },
        "source": {
            "crs": source_crs_label,
            "sha256": sha256(source_path),
            "featureCount": len(features),
            "baseDate": properties["BASE_DATE"],
            "administrativeCode": properties["SIDO_CD"],
            "administrativeName": properties["SIDO_NM"],
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


def read_provenance(payload: dict[str, Any]) -> dict[str, Any]:
    provenance = payload.get("_provenance")
    if not isinstance(provenance, dict):
        raise TypeError("source GeoJSON _provenance must be an object")
    expected_values: dict[str, Any] = {
        "pageUrl": SOURCE_PAGE_URL,
        "datasetName": DATASET_NAME,
        "referencePeriod": REFERENCE_PERIOD,
        "fileIdentifier": FILE_IDENTIFIER,
        "detailNumber": 1,
        "sourceLayer": SOURCE_LAYER,
        "sourceLayerCrs": SOURCE_LAYER_CRS,
    }
    for key, expected in expected_values.items():
        if provenance.get(key) != expected:
            raise ValueError(f"unexpected source provenance {key}")
    archive_sha256 = provenance.get("archiveSha256")
    if (
        not isinstance(archive_sha256, str)
        or len(archive_sha256) != 64
        or any(character not in "0123456789abcdef" for character in archive_sha256)
    ):
        raise ValueError("source provenance archiveSha256 must be lowercase SHA-256")
    if not isinstance(provenance.get("sourceLayerFeatureCount"), int):
        raise TypeError("source provenance feature count must be an integer")
    normalize_collected_at(cast(str, provenance.get("collectedAt")))
    return provenance


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
                "geometry": rounded_mapping(geometry),
            }
        ],
    }
    write_json(path, payload, compact=True)


def write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    compact: bool = False,
) -> None:
    path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rounded_mapping(geometry: BaseGeometry) -> dict[str, Any]:
    geometry_mapping = dict(mapping(geometry))
    geometry_mapping["coordinates"] = round_coordinates(geometry_mapping["coordinates"])
    return geometry_mapping


def round_coordinates(value: Any) -> Any:
    if isinstance(value, (float, int)):
        return round(float(value), COORDINATE_PRECISION)
    return [round_coordinates(child) for child in value]


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
