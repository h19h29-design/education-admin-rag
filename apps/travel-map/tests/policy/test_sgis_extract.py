import hashlib
import json
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape

SCRIPT = Path("apps/travel-map/scripts/extract-sgis-seoul.py")
SOURCE_WKT = (
    'PROJCS["Korea_2000_Korea_Unified_Coordinate_System",'
    'GEOGCS["GCS_Korea_2000",DATUM["D_Korea_2000",'
    'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",1000000.0],'
    'PARAMETER["False_Northing",2000000.0],PARAMETER["Central_Meridian",127.5],'
    'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",38.0],'
    'UNIT["Meter",1.0]]'
)


# Production break caught: extracting a province other than observed SIDO_CD 11.
def test_extractor_selects_only_seoul_and_transforms_observed_crs(
    tmp_path: Path,
) -> None:
    archive = make_sgis_archive(tmp_path)
    output = tmp_path / "seoul-boundary.geojson"

    completed = subprocess.run(
        extractor_command(archive, output),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["features"]) == 1
    assert payload["features"][0]["properties"] == {
        "BASE_DATE": "20250630",
        "SIDO_CD": "11",
        "SIDO_NM": "서울특별시",
    }
    geometry = shape(payload["features"][0]["geometry"])
    assert geometry.covers(Point(126.9780, 37.5665))
    assert not geometry.covers(Point(129.05, 35.15))
    assert payload["_provenance"] == {
        "pageUrl": "https://www.data.go.kr/data/15129688/fileData.do",
        "datasetName": "국가데이터처_SGIS 행정구역 통계 및 경계_20250630",
        "referencePeriod": "2025 Q2",
        "fileIdentifier": "FILE_000000003681593",
        "detailNumber": 1,
        "archiveSha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "sourceLayer": "bnd_sido_00_2025_2Q",
        "sourceLayerCrs": {
            "authority": "ESRI:102080",
            "name": "Korea_2000_Korea_Unified_Coordinate_System",
        },
        "sourceLayerFeatureCount": 2,
        "collectedAt": "2026-08-10T08:00:00Z",
    }


# Production break caught: silently transforming an unverified shapefile CRS.
def test_extractor_rejects_unexpected_source_crs(tmp_path: Path) -> None:
    archive = make_sgis_archive(tmp_path, source_wkt=CRS.from_epsg(4326).to_wkt())

    completed = subprocess.run(
        extractor_command(archive, tmp_path / "seoul-boundary.geojson"),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "source CRS must be ESRI:102080" in completed.stderr


def make_sgis_archive(tmp_path: Path, *, source_wkt: str = SOURCE_WKT) -> Path:
    shape_root = tmp_path / "shape"
    shape_root.mkdir()
    base = shape_root / "bnd_sido_00_2025_2Q"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="utf-8")
    writer.field("BASE_DATE", "C", size=8)
    writer.field("SIDO_CD", "C", size=2)
    writer.field("SIDO_NM", "C", size=25)
    transformer = Transformer.from_crs(
        "OGC:CRS84", CRS.from_wkt(SOURCE_WKT), always_xy=True
    )
    write_feature(
        writer,
        transformer,
        ring=[
            (126.90, 37.50),
            (126.90, 37.60),
            (127.00, 37.60),
            (127.00, 37.50),
            (126.90, 37.50),
        ],
        code="11",
        name="서울특별시",
    )
    write_feature(
        writer,
        transformer,
        ring=[
            (129.00, 35.10),
            (129.00, 35.20),
            (129.10, 35.20),
            (129.10, 35.10),
            (129.00, 35.10),
        ],
        code="21",
        name="부산광역시",
    )
    writer.close()
    base.with_suffix(".prj").write_text(source_wkt, encoding="utf-8")
    base.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")

    archive = tmp_path / "sgis.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zip_file:
        for extension in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            zip_file.write(
                base.with_suffix(extension),
                arcname=f"nested/source/{base.name}{extension}",
            )
    return archive


def write_feature(
    writer: shapefile.Writer,
    transformer: Transformer,
    *,
    ring: list[tuple[float, float]],
    code: str,
    name: str,
) -> None:
    writer.poly(
        [[transformer.transform(longitude, latitude) for longitude, latitude in ring]]
    )
    writer.record("20250630", code, name)


def extractor_command(archive: Path, output: Path) -> list[str]:
    return [
        sys.executable,
        str(SCRIPT),
        "--archive",
        str(archive),
        "--output",
        str(output),
        "--collected-at",
        "2026-08-10T08:00:00Z",
    ]
