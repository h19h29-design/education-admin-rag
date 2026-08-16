import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, shape  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import transform, unary_union  # type: ignore[import-untyped]

from app.policy.models import CoverageState
from app.routing.models import Coordinate

_SHA256 = re.compile(r"[0-9a-f]{64}")
_EXPECTED_OUTPUTS = frozenset({"seoul.geojson", "seoul-plus-12km.geojson"})


@dataclass(frozen=True)
class VerifiedGeodata:
    """Normalized geodata whose bytes match the reviewed manifest."""

    seoul_geojson: bytes
    support_geojson: bytes


class CoverageService:
    def __init__(
        self,
        *,
        seoul_projected: BaseGeometry,
        buffer_distance_m: int,
        wgs84_to_projected: Transformer,
    ) -> None:
        self._seoul = seoul_projected
        self._buffer_distance_m = buffer_distance_m
        self._wgs84_to_projected = wgs84_to_projected

    @classmethod
    def from_geojson(
        cls,
        *,
        seoul_path: str | Path,
        buffer_distance_m: int,
    ) -> "CoverageService":
        payload: dict[str, Any] = json.loads(
            Path(seoul_path).read_text(encoding="utf-8")
        )
        geometries = [shape(feature["geometry"]) for feature in payload["features"]]
        seoul_wgs84 = unary_union(geometries)
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
        seoul_projected = transform(transformer.transform, seoul_wgs84)
        return cls(
            seoul_projected=seoul_projected,
            buffer_distance_m=buffer_distance_m,
            wgs84_to_projected=transformer,
        )

    @classmethod
    def from_resources(cls, root: str | Path, *, verify_source: bool) -> "CoverageService":
        """Load only normalized outputs whose reviewed manifest is intact."""

        verified = verify_geodata_resources(root, verify_source=verify_source)
        return cls._from_geojson_bytes(verified.seoul_geojson, "seoul.geojson")

    @classmethod
    def _from_geojson_bytes(cls, data: bytes, label: str) -> "CoverageService":
        payload = _parse_feature_collection(data, label)
        geometries = [shape(feature["geometry"]) for feature in payload["features"]]
        seoul_wgs84 = unary_union(geometries)
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:5179", always_xy=True)
        seoul_projected = transform(transformer.transform, seoul_wgs84)
        return cls(
            seoul_projected=seoul_projected,
            buffer_distance_m=12_000,
            wgs84_to_projected=transformer,
        )

    def classify(self, point: Coordinate) -> CoverageState:
        projected_point = transform(
            self._wgs84_to_projected.transform,
            Point(point.longitude, point.latitude),
        )
        if self._seoul.covers(projected_point):
            return CoverageState.SEOUL
        if self._seoul.distance(projected_point) <= self._buffer_distance_m:
            return CoverageState.BUFFER
        return CoverageState.OUTSIDE


def verify_geodata_resources(
    root: str | Path,
    *,
    verify_source: bool,
) -> VerifiedGeodata:
    """Verify reviewed normalized outputs, and source bytes when available.

    The runtime image deliberately excludes the raw SGIS input, so source-byte
    verification happens in the release workspace before the allowlisted build
    context is created. Both environments verify the shipped output hashes.
    """

    root_path = Path(root)
    manifest = _read_manifest(root_path / "manifest.json")
    outputs = manifest["outputs"]
    if type(outputs) is not dict or set(outputs) != _EXPECTED_OUTPUTS:
        raise ValueError("geodata manifest outputs are invalid")

    verified: dict[str, bytes] = {}
    for filename in sorted(_EXPECTED_OUTPUTS):
        metadata = outputs[filename]
        if type(metadata) is not dict or set(metadata) != {
            "crs",
            "featureCount",
            "sha256",
        }:
            raise ValueError(f"geodata manifest metadata is invalid: {filename}")
        expected_hash = metadata["sha256"]
        if type(expected_hash) is not str or _SHA256.fullmatch(expected_hash) is None:
            raise ValueError(f"geodata manifest sha256 is invalid: {filename}")
        if metadata["crs"] != "OGC:CRS84" or type(metadata["featureCount"]) is not int:
            raise ValueError(f"geodata manifest metadata is invalid: {filename}")
        data = _read_bytes(root_path / filename, filename)
        _verify_hash(data, expected_hash, filename)
        payload = _parse_feature_collection(data, filename)
        if len(payload["features"]) != metadata["featureCount"]:
            raise ValueError(f"geodata feature count mismatch: {filename}")
        verified[filename] = data

    source = manifest["source"]
    if type(source) is not dict or type(source.get("sha256")) is not str:
        raise ValueError("geodata manifest source is invalid")
    source_hash = source["sha256"]
    if _SHA256.fullmatch(source_hash) is None:
        raise ValueError("geodata manifest source sha256 is invalid")
    if verify_source:
        source_data = _read_bytes(
            root_path / "source/seoul-boundary.geojson",
            "source/seoul-boundary.geojson",
        )
        _verify_hash(source_data, source_hash, "source")

    return VerifiedGeodata(
        seoul_geojson=verified["seoul.geojson"],
        support_geojson=verified["seoul-plus-12km.geojson"],
    )


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("geodata manifest is unavailable") from exc
    if type(payload) is not dict or set(payload) != {
        "coverage",
        "generatedAt",
        "outputs",
        "source",
        "sourceArchive",
    }:
        raise ValueError("geodata manifest is invalid")
    return payload


def _read_bytes(path: Path, label: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"geodata artifact is unavailable: {label}") from exc


def _verify_hash(data: bytes, expected: str, label: str) -> None:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise ValueError(f"geodata {label} sha256 mismatch")


def _parse_feature_collection(data: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"geodata artifact is invalid: {label}") from exc
    if (
        type(payload) is not dict
        or payload.get("type") != "FeatureCollection"
        or type(payload.get("features")) is not list
        or not payload["features"]
        or any(type(feature) is not dict for feature in payload["features"])
    ):
        raise ValueError(f"geodata artifact is invalid: {label}")
    return payload
