import json
from pathlib import Path
from typing import Any

from pyproj import Transformer
from shapely.geometry import Point, shape  # type: ignore[import-untyped]
from shapely.geometry.base import BaseGeometry  # type: ignore[import-untyped]
from shapely.ops import transform, unary_union  # type: ignore[import-untyped]

from app.policy.models import CoverageState
from app.routing.models import Coordinate


class CoverageService:
    def __init__(
        self,
        *,
        seoul_projected: BaseGeometry,
        support_area_projected: BaseGeometry,
        wgs84_to_projected: Transformer,
    ) -> None:
        self._seoul = seoul_projected
        self._support_area = support_area_projected
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
            support_area_projected=seoul_projected.buffer(buffer_distance_m),
            wgs84_to_projected=transformer,
        )

    def classify(self, point: Coordinate) -> CoverageState:
        projected_point = transform(
            self._wgs84_to_projected.transform,
            Point(point.longitude, point.latitude),
        )
        if self._seoul.covers(projected_point):
            return CoverageState.SEOUL
        if self._support_area.covers(projected_point):
            return CoverageState.BUFFER
        return CoverageState.OUTSIDE
