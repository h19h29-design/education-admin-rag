import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.routing.models import (
    CarAssumptions,
    Coordinate,
    FuelType,
    RouteQuery,
    TravelMode,
)

FIXTURES = Path("apps/travel-map/tests/fixtures/providers")
NOW = datetime(2026, 8, 10, 9, 1, tzinfo=ZoneInfo("Asia/Seoul"))


def load_json(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def route_query(
    mode: TravelMode,
    *,
    car_assumptions: CarAssumptions | None = None,
) -> RouteQuery:
    return RouteQuery(
        origin=Coordinate(37.55, 126.97),
        destination=Coordinate(37.56, 126.98),
        depart_at=NOW,
        mode=mode,
        car_assumptions=car_assumptions,
    )


def gasoline_assumptions() -> CarAssumptions:
    return CarAssumptions(
        fuel_type=FuelType.GASOLINE,
        efficiency_km_per_liter=10.0,
        parking_cost_krw=2_000,
    )
