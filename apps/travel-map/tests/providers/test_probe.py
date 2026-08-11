import argparse
import asyncio
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import httpx
from app.routing.models import Coordinate
from app.settings import Settings
from pydantic import SecretStr
from tests.providers.helpers import FIXTURES, load_json

SCRIPT = Path("apps/travel-map/scripts/probe-route-providers.py").resolve()


def test_live_probe_skips_atomically_without_keys_and_never_writes_raw_payload(
    tmp_path: Path,
) -> None:
    output = tmp_path / "provider-contract-report.json"
    environment = {
        **os.environ,
        "TRAVEL_MAP_LIVE_SMOKE": "1",
    }
    for name in (
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
    ):
        environment.pop(name, None)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--origin",
            "126.9779451,37.5662952",
            "--destination",
            "126.9910,37.5512",
            "--output",
            str(output),
        ],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "SKIPPED_MISSING_CREDENTIALS"
    assert report["missingCredentials"] == [
        "KAKAO_REST_API_KEY",
        "OPINET_CERT_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
    ]
    assert output.stat().st_size < 20_000
    assert not list(tmp_path.glob("*.tmp"))
    text = output.read_text(encoding="utf-8")
    assert "rawPayload" not in text
    assert "Authorization" not in text


def test_live_probe_missing_check_reads_dotenv_through_settings(tmp_path: Path) -> None:
    output = tmp_path / "provider-contract-report.json"
    (tmp_path / ".env").write_text(
        "KAKAO_REST_API_KEY=dotenv-only-key\n",
        encoding="utf-8",
    )
    environment = {**os.environ, "TRAVEL_MAP_LIVE_SMOKE": "1"}
    for name in (
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
    ):
        environment.pop(name, None)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--origin",
            "126.9779451,37.5662952",
            "--destination",
            "126.9910,37.5512",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["missingCredentials"] == [
        "OPINET_CERT_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
    ]


def test_live_probe_calls_every_integration_and_records_observed_counts() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        path = request.url.path
        if path.endswith("/search/keyword.json"):
            payload: object = load_json("kakao-keyword.json")
            content_type = "application/json"
        elif path.endswith("/geo/coord2address.json"):
            payload = load_json("kakao-coord2address.json")
            content_type = "application/json"
        elif path.endswith("/routing/publictraffic"):
            payload = load_json("kakao-publictraffic.json")
            content_type = "application/json"
        elif path.endswith("/routing/walk"):
            payload = load_json("kakao-walk.json")
            content_type = "application/json"
        elif path.endswith("/v1/directions"):
            assert request.url.params["car_fuel"] == "GASOLINE"
            payload = load_json("kakao-car.json")
            content_type = "application/json"
        elif path.endswith("/avgAllPrice.do"):
            payload = load_json("opinet-average.json")
            content_type = "application/json"
        elif path.endswith("/getPathInfoByBusNSub"):
            return httpx.Response(
                200,
                headers={"Content-Type": "application/xml"},
                content=(FIXTURES / "seoul-transit.xml").read_bytes(),
            )
        else:
            raise AssertionError(path)
        return httpx.Response(
            200,
            headers={"Content-Type": content_type},
            json=payload,
        )

    module = runpy.run_path(str(SCRIPT), run_name="provider_probe_test")
    probe = module["_probe"]
    args = argparse.Namespace(
        origin=Coordinate(37.5662952, 126.9779451),
        destination=Coordinate(37.5512, 126.991),
        output=Path("unused"),
    )
    settings = Settings(
        kakao_rest_api_key=SecretStr("kakao"),
        seoul_transit_service_key=SecretStr("seoul"),
        opinet_cert_key=SecretStr("opinet"),
        _env_file=None,
    )

    async def run_probe() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await probe(args, settings=settings, http=http)

    report = asyncio.run(run_probe())

    assert set(calls) == {
        "/v2/local/search/keyword.json",
        "/v2/local/geo/coord2address.json",
        "/v2/routing/publictraffic",
        "/v2/routing/walk",
        "/v1/directions",
        "/api/avgAllPrice.do",
        "/api/rest/pathinfo/getPathInfoByBusNSub",
    }
    operations = {item["operation"]: item for item in report["operations"]}  # type: ignore[index]
    assert operations["KAKAO_LOCAL_SEARCH"]["placeCount"] == 2
    assert operations["KAKAO_TRANSIT"]["routeCount"] == 3
    assert len(operations["KAKAO_TRANSIT"]["routes"]) == 3
    assert operations["KAKAO_CAR"]["routeCount"] == 2
    assert operations["OPINET"]["httpStatus"] == 200
    assert all(len(item["schemaFingerprint"]) == 64 for item in operations.values())
