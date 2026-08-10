import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import shapefile
from pyproj import CRS, Transformer
from shapely.geometry import Point, shape

SCRIPT = Path("apps/travel-map/scripts/extract-sgis-seoul.py")
PRODUCTION_SOURCE = Path(
    "apps/travel-map/resources/geodata/source/seoul-boundary.geojson"
)
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


# Production break caught: breaking generic SGIS geometry inspection while
# separating synthetic fixtures from the official provenance-producing path.
def test_unverified_geometry_reader_selects_seoul_and_transforms_observed_crs(
    tmp_path: Path,
) -> None:
    archive = make_sgis_archive(tmp_path)
    extracted = load_extractor_module().read_sgis_geometry_without_official_provenance(
        archive
    )

    assert extracted.record == {
        "BASE_DATE": "20250630",
        "SIDO_CD": "11",
        "SIDO_NM": "서울특별시",
    }
    assert extracted.geometry.covers(Point(126.9780, 37.5665))
    assert not extracted.geometry.covers(Point(129.05, 35.15))
    assert extracted.source_layer_feature_count == 2


# Production break caught: assigning official SGIS identifiers to a structurally
# valid archive whose bytes do not match the pinned official download.
def test_official_extractor_rejects_structurally_valid_wrong_hash_archive(
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

    assert completed.returncode != 0
    assert "archive SHA-256 does not match the pinned official SGIS file" in completed.stderr
    assert not output.exists()


# Production break caught: leaving the successful public extraction path
# unverified across its hash gate, official payload assembly, and file write.
def test_public_extractor_writes_deterministic_official_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_source_before = PRODUCTION_SOURCE.read_bytes()
    archive = make_sgis_archive(tmp_path)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    extractor = load_extractor_module()
    monkeypatch.setattr(extractor, "OFFICIAL_ARCHIVE_SHA256", archive_sha256)
    first_output = tmp_path / "first.geojson"
    second_output = tmp_path / "second.geojson"

    extractor.extract_seoul(
        archive_path=archive,
        output_path=first_output,
        collected_at="2026-08-10T08:00:00Z",
    )
    extractor.extract_seoul(
        archive_path=archive,
        output_path=second_output,
        collected_at="2026-08-10T08:00:00Z",
    )

    assert first_output.read_bytes() == second_output.read_bytes()
    payload = json.loads(first_output.read_text(encoding="utf-8"))
    assert payload["_provenance"] == {
        "pageUrl": "https://www.data.go.kr/data/15129688/fileData.do",
        "datasetName": "국가데이터처_SGIS 행정구역 통계 및 경계_20250630",
        "referencePeriod": "2025 Q2",
        "fileIdentifier": "FILE_000000003681593",
        "detailNumber": 1,
        "archiveSha256": archive_sha256,
        "sourceLayer": "bnd_sido_00_2025_2Q",
        "sourceLayerCrs": {
            "authority": "ESRI:102080",
            "name": "Korea_2000_Korea_Unified_Coordinate_System",
        },
        "sourceLayerFeatureCount": 2,
        "collectedAt": "2026-08-10T08:00:00Z",
    }
    assert payload["crs"] == {
        "type": "name",
        "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"},
    }
    assert payload["features"][0]["properties"] == {
        "BASE_DATE": "20250630",
        "SIDO_CD": "11",
        "SIDO_NM": "서울특별시",
    }
    geometry = shape(payload["features"][0]["geometry"])
    assert geometry.covers(Point(126.9780, 37.5665))
    assert not geometry.covers(Point(129.05, 35.15))
    assert PRODUCTION_SOURCE.read_bytes() == production_source_before


# Production break caught: silently transforming an unverified shapefile CRS.
def test_extractor_rejects_unexpected_source_crs(tmp_path: Path) -> None:
    archive = make_sgis_archive(tmp_path, source_wkt=CRS.from_epsg(4326).to_wkt())

    with pytest.raises(ValueError, match="source CRS must be ESRI:102080"):
        load_extractor_module().read_sgis_geometry_without_official_provenance(archive)


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


def load_extractor_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("extract_sgis_seoul", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
