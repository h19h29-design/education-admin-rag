import json
import os
import subprocess
import sys
from pathlib import Path


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
            "apps/travel-map/scripts/probe-route-providers.py",
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
