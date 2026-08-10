#!/usr/bin/env python3
import argparse
import hashlib
import json
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zipfile import ZipFile, ZipInfo

import shapefile  # type: ignore[import-untyped]
from pyproj import CRS, Transformer
from shapely import make_valid  # type: ignore[import-untyped]
from shapely.geometry import (  # type: ignore[import-untyped]
    GeometryCollection,
    MultiPolygon,
    Point,
    Polygon,
    mapping,
    shape,
)
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import transform, unary_union  # type: ignore[import-untyped]

PAGE_URL = "https://www.data.go.kr/data/15129688/fileData.do"
DATASET_NAME = "국가데이터처_SGIS 행정구역 통계 및 경계_20250630"
REFERENCE_PERIOD = "2025 Q2"
FILE_IDENTIFIER = "FILE_000000003681593"
DETAIL_NUMBER = 1
OFFICIAL_ARCHIVE_SHA256 = (
    "f1cf0f9de453ac7eaacb273f39cee52851183372b9ddfda428a967c3a670b2c6"
)
SOURCE_LAYER = "bnd_sido_00_2025_2Q"
EXPECTED_CRS_AUTHORITY = ("ESRI", "102080")
EXPECTED_CRS_NAME = "Korea_2000_Korea_Unified_Coordinate_System"
EXPECTED_FIELDS = (
    ("BASE_DATE", "C", 8, 0),
    ("SIDO_CD", "C", 2, 0),
    ("SIDO_NM", "C", 25, 0),
)
SEOUL_CODE = "11"
SEOUL_NAME = "서울특별시"
BASE_DATE = "20250630"
SEOUL_CITY_HALL = Point(126.9780, 37.5665)
COORDINATE_PRECISION = 7
REQUIRED_EXTENSIONS = (".shp", ".shx", ".dbf", ".prj", ".cpg")
MAX_MEMBER_BYTES = {
    ".shp": 200_000_000,
    ".shx": 1_000_000,
    ".dbf": 10_000_000,
    ".prj": 100_000,
    ".cpg": 100,
}


@dataclass(frozen=True)
class ValidatedSgisGeometry:
    record: dict[str, str]
    geometry: BaseGeometry
    source_crs_name: str
    source_layer_feature_count: int


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract the Seoul feature from the official SGIS province layer."
    )
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--collected-at",
        help="UTC ISO-8601 archive collection time; defaults to the current time.",
    )
    args = parser.parse_args()

    try:
        extract_seoul(
            archive_path=args.archive,
            output_path=args.output,
            collected_at=normalize_timestamp(args.collected_at),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        parser.error(str(error))


def extract_seoul(
    *,
    archive_path: Path,
    output_path: Path,
    collected_at: str,
) -> None:
    archive_sha256 = sha256(archive_path)
    if archive_sha256 != OFFICIAL_ARCHIVE_SHA256:
        raise ValueError(
            "archive SHA-256 does not match the pinned official SGIS file"
        )
    extracted = read_sgis_geometry_without_official_provenance(archive_path)
    payload = {
        "type": "FeatureCollection",
        "name": "seoul-boundary-sgis-2025-q2",
        "_provenance": {
            "pageUrl": PAGE_URL,
            "datasetName": DATASET_NAME,
            "referencePeriod": REFERENCE_PERIOD,
            "fileIdentifier": FILE_IDENTIFIER,
            "detailNumber": DETAIL_NUMBER,
            "archiveSha256": archive_sha256,
            "sourceLayer": SOURCE_LAYER,
            "sourceLayerCrs": {
                "authority": ":".join(EXPECTED_CRS_AUTHORITY),
                "name": extracted.source_crs_name,
            },
            "sourceLayerFeatureCount": extracted.source_layer_feature_count,
            "collectedAt": collected_at,
        },
        "crs": {
            "type": "name",
            "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
        },
        "features": [
            {
                "type": "Feature",
                "properties": extracted.record,
                "geometry": rounded_mapping(extracted.geometry),
            }
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, payload, compact=True)


def read_sgis_geometry_without_official_provenance(
    archive_path: Path,
) -> ValidatedSgisGeometry:
    with ZipFile(archive_path) as zip_file, TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        members = find_layer_members(zip_file)
        copy_layer_members(zip_file, members, temporary_root)
        base = temporary_root / SOURCE_LAYER
        encoding = base.with_suffix(".cpg").read_text(encoding="ascii").strip()
        if encoding.upper() != "UTF-8":
            raise ValueError("source CPG must declare UTF-8")

        source_crs = CRS.from_wkt(base.with_suffix(".prj").read_text(encoding="utf-8"))
        if source_crs.to_authority() != EXPECTED_CRS_AUTHORITY:
            raise ValueError("source CRS must be ESRI:102080")
        if source_crs.name != EXPECTED_CRS_NAME:
            raise ValueError(f"unexpected source CRS name: {source_crs.name}")

        reader = shapefile.Reader(str(base), encoding="utf-8")
        validate_reader(reader)
        matching = [
            shape_record
            for shape_record in reader.iterShapeRecords()
            if shape_record.record.as_dict()["SIDO_CD"] == SEOUL_CODE
        ]
        if len(matching) != 1:
            raise ValueError(f"expected one Seoul feature, found {len(matching)}")
        record = matching[0].record.as_dict()
        if record != {
            "BASE_DATE": BASE_DATE,
            "SIDO_CD": SEOUL_CODE,
            "SIDO_NM": SEOUL_NAME,
        }:
            raise ValueError(f"unexpected Seoul record: {record}")

        projected_geometry = polygonal_geometry(
            shape(matching[0].shape.__geo_interface__)
        )
        to_wgs84 = Transformer.from_crs(source_crs, "OGC:CRS84", always_xy=True)
        seoul_wgs84 = polygonal_geometry(
            transform(to_wgs84.transform, projected_geometry)
        )
        if not seoul_wgs84.covers(SEOUL_CITY_HALL):
            raise ValueError("extracted boundary does not contain Seoul City Hall")

        return ValidatedSgisGeometry(
            record=record,
            geometry=seoul_wgs84,
            source_crs_name=source_crs.name,
            source_layer_feature_count=len(reader),
        )


def find_layer_members(zip_file: ZipFile) -> dict[str, ZipInfo]:
    members: dict[str, ZipInfo] = {}
    for extension in REQUIRED_EXTENSIONS:
        expected_name = f"{SOURCE_LAYER}{extension}"
        matches = [
            info
            for info in zip_file.infolist()
            if not info.is_dir() and Path(info.filename).name == expected_name
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one {expected_name} archive member")
        member = matches[0]
        if member.file_size > MAX_MEMBER_BYTES[extension]:
            raise ValueError(f"archive member is unexpectedly large: {expected_name}")
        members[extension] = member
    return members


def copy_layer_members(
    zip_file: ZipFile,
    members: dict[str, ZipInfo],
    output_directory: Path,
) -> None:
    for extension, member in members.items():
        output_path = output_directory / f"{SOURCE_LAYER}{extension}"
        with zip_file.open(member) as source, output_path.open("wb") as destination:
            shutil.copyfileobj(source, destination)


def validate_reader(reader: shapefile.Reader) -> None:
    if reader.shapeType != shapefile.POLYGON:
        raise ValueError("source layer must use Polygon shapes")
    actual_fields = tuple(
        (field.name, str(field.field_type), field.size, field.decimal)
        for field in reader.fields[1:]
    )
    if actual_fields != EXPECTED_FIELDS:
        raise ValueError(f"unexpected DBF fields: {actual_fields}")


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


def rounded_mapping(geometry: BaseGeometry) -> dict[str, Any]:
    geometry_mapping = dict(mapping(geometry))
    geometry_mapping["coordinates"] = round_coordinates(geometry_mapping["coordinates"])
    return geometry_mapping


def round_coordinates(value: Any) -> Any:
    if isinstance(value, (float, int)):
        return round(float(value), COORDINATE_PRECISION)
    return [round_coordinates(child) for child in value]


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


def normalize_timestamp(value: str | None) -> str:
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
