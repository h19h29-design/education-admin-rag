import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.contracts import TripPreviewResponse
from app.institutions.snapshot import verify_snapshot
from app.policy.coverage import CoverageService
from app.policy.models import CoverageState
from app.policy.rules import RuleRepository

ROOT = Path("apps/travel-map")
SMOKE = ROOT / "scripts/smoke-live.py"
PREPARE_CONTEXT = ROOT / "scripts/prepare-release-context.py"
SYNC = ROOT / "scripts/sync-institutions.py"
FIXTURE_SNAPSHOT = ROOT / "tests/fixtures/institutions/snapshot"


# Production break caught: a release check treating the intentionally absent
# production snapshot as an empty-but-deployable institution catalog.
def test_release_preflight_blocks_when_the_production_snapshot_is_absent() -> None:
    completed = _run_smoke(
        {
            "TRAVEL_MAP_LIVE_SMOKE": "1",
            "KAKAO_REST_API_KEY": "test-rest",
            "SEOUL_TRANSIT_SERVICE_KEY": "test-transit",
            "OPINET_CERT_KEY": "test-opinet",
        }
    )

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report == {"status": "BLOCKED_MISSING_APPROVED_SNAPSHOT"}


# Production break caught: release verification claiming success from a synthetic
# test fixture copied into the production resource location.
def test_verified_resource_success_uses_the_existing_test_fixture_only() -> None:
    snapshot = verify_snapshot(FIXTURE_SNAPSHOT)

    assert snapshot.manifest.approved is True
    assert snapshot.manifest.approved_by_role == "TEST_FIXTURE_REVIEWER"
    coverage = CoverageService.from_geojson(
        seoul_path=ROOT / "resources/geodata/seoul.geojson",
        buffer_distance_m=12_000,
    )
    assert coverage is not None


# Production break caught: a syntactically valid normalized boundary changed
# after review can pass JSON parsing and still alter the support area at startup.
@pytest.mark.parametrize(
    "relative_path",
    ("seoul.geojson", "seoul-plus-12km.geojson"),
)
def test_release_geodata_preflight_rejects_tampered_normalized_output(
    tmp_path: Path,
    relative_path: str,
) -> None:
    geodata_root = tmp_path / "geodata"
    shutil.copytree(ROOT / "resources/geodata", geodata_root)
    artifact = geodata_root / relative_path
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["features"][0]["properties"]["name"] = "변조된 경계"
    artifact.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        CoverageService.from_resources(geodata_root, verify_source=True)


# Production break caught: release provenance can claim a reviewed SGIS source
# while the local source used to make the normalized outputs has been replaced.
def test_release_geodata_preflight_rejects_tampered_recorded_source(
    tmp_path: Path,
) -> None:
    geodata_root = tmp_path / "geodata"
    shutil.copytree(ROOT / "resources/geodata", geodata_root)
    source = geodata_root / "source/seoul-boundary.geojson"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["_provenance"]["administrativeName"] = "변조된 원본"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="source sha256 mismatch"):
        CoverageService.from_resources(geodata_root, verify_source=True)


# Production break caught: a syntactically valid, materially altered rule file
# can otherwise be accepted because the version index does not bind its bytes.
def test_rule_manifest_rejects_tampered_rule_payload(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    shutil.copytree(ROOT / "resources/rules", rules_root)
    rule_path = rules_root / "local-travel-2026-07-01.json"
    payload = json.loads(rule_path.read_text(encoding="utf-8"))
    payload["fourHoursOrMoreKrw"] = 20_001
    rule_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="rule sha256 mismatch"):
        RuleRepository.from_directory(rules_root)


# Production break caught: an unpinned rule index can parse a reviewed-looking
# payload but leaves its exact policy bytes unbound during release startup.
def test_production_rule_preflight_requires_a_hash_for_every_rule(tmp_path: Path) -> None:
    rules_root = tmp_path / "rules"
    shutil.copytree(ROOT / "resources/rules", rules_root)
    index_path = rules_root / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["rules"][0]["sha256"]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(ValueError, match="rule index must pin every rule sha256"):
        RuleRepository.from_directory(rules_root, require_hashes=True)


# Production break caught: a Docker build context that transfers every prior
# approved snapshot even though the image copies only the one selected today.
def test_release_context_contains_only_the_current_verified_snapshot(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "travel-map"
    shutil.copytree(ROOT, source_root)
    snapshots = source_root / "resources/institution-snapshots"
    snapshots.mkdir()
    shutil.copytree(
        FIXTURE_SNAPSHOT / "fixture-001",
        snapshots / "fixture-001",
    )
    (snapshots / "current.json").write_text(
        json.dumps({"snapshotId": "fixture-001"}),
        encoding="utf-8",
    )
    shutil.copytree(snapshots / "fixture-001", snapshots / "historical-001")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    stage_release_context = module["stage_release_context"]
    context_root = tmp_path / "context"
    staged_snapshot_id = stage_release_context(source_root, context_root)

    assert staged_snapshot_id == "fixture-001"
    context_snapshots = context_root / "resources/institution-snapshots"
    assert sorted(
        path.relative_to(context_snapshots).as_posix()
        for path in context_snapshots.rglob("*")
    ) == [
        "current.json",
        "fixture-001",
        "fixture-001/institutions.jsonl",
        "fixture-001/manifest.json",
        "fixture-001/sites.jsonl",
    ]
    assert not (context_root / "resources/geodata/source").exists()
    assert not (context_root / "resources/institution-sources").exists()
    assert not (context_root / "tests").exists()
    assert not (context_root / "e2e").exists()


def test_release_context_omits_unlisted_files_from_selected_snapshot_and_rules(
    tmp_path: Path,
) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    snapshot = source_root / "resources/institution-snapshots/fixture-001"
    (snapshot / "secrets.txt").write_text("not for Docker", encoding="utf-8")
    (snapshot / "review-notes.md").write_text("not for Docker", encoding="utf-8")
    rules = source_root / "resources/rules"
    (rules / "unlisted-rule.json").write_text("not for Docker", encoding="utf-8")
    (rules / "review-notes.md").write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](source_root, context_root)

    staged_snapshot = context_root / "resources/institution-snapshots/fixture-001"
    staged_rules = context_root / "resources/rules"
    assert sorted(path.name for path in staged_snapshot.iterdir()) == [
        "institutions.jsonl",
        "manifest.json",
        "sites.jsonl",
    ]
    assert sorted(path.name for path in staged_rules.iterdir()) == [
        "index.json",
        "local-travel-2026-07-01.json",
    ]


# Production break caught: a broad application-tree copy leaks nested dotenvs,
# raw inputs, test assets, caches, or Git data into a Docker build context.
def test_release_context_allowlists_only_production_app_files(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    app_root = source_root / "app"
    malicious_paths = (
        ".env",
        "nested/.env",
        "nested/.env.production",
        "raw/provider-response.json",
        "source/archive.geojson",
        "artifacts/report.json",
        "tests/test_hidden.py",
        "e2e/trace.zip",
        "__pycache__/main.cpython-312.pyc",
        ".git/config",
    )
    for relative_path in malicious_paths:
        path = app_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](source_root, context_root)

    staged_app = context_root / "app"
    assert (context_root / ".dockerignore").is_file()
    assert (staged_app / "main.py").is_file()
    assert (staged_app / "static/index.html").is_file()
    assert all(not (staged_app / relative_path).exists() for relative_path in malicious_paths)
    assert all(
        path.suffix == ".py" or path.relative_to(staged_app).parts[0] == "static"
        for path in staged_app.rglob("*")
        if path.is_file()
    )


# Production break caught: suffix-only application allowlisting can stage a
# hidden Python module or static payload, including ones beneath a hidden path.
def test_release_context_omits_every_hidden_application_path(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    app_root = source_root / "app"
    hidden_paths = (
        ".secret.py",
        ".private/module.py",
        "static/.secret.js",
        "static/.private/bundle.css",
    )
    for relative_path in hidden_paths:
        path = app_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not for Docker", encoding="utf-8")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")
    context_root = tmp_path / "context"
    module["stage_release_context"](source_root, context_root)

    staged_app = context_root / "app"
    assert (staged_app / "main.py").is_file()
    assert (staged_app / "static/index.html").is_file()
    assert all(not (staged_app / relative_path).exists() for relative_path in hidden_paths)
    assert all(
        not any(part.startswith(".") for part in path.relative_to(staged_app).parts)
        for path in staged_app.rglob("*")
    )


# Production break caught: a symlink in a copied application path can resolve
# outside the reviewed source tree while bypassing the context allowlist.
def test_release_context_rejects_an_application_symlink(tmp_path: Path) -> None:
    source_root = _release_source_with_current_snapshot(tmp_path)
    link = source_root / "app/nested-link.py"
    link.symlink_to(source_root / "app/main.py")

    module = runpy.run_path(str(PREPARE_CONTEXT), run_name="release_context_test")

    with pytest.raises(ValueError, match="symlink"):
        module["stage_release_context"](source_root, tmp_path / "context")


# Production break caught: enabling a billed live check by accident or allowing
# a credential/snapshot error to disclose a secret or institution identifier.
def test_live_smoke_refuses_unapproved_execution_with_a_safe_report() -> None:
    completed = _run_smoke({})

    assert completed.returncode == 2
    report = json.loads(completed.stdout)
    assert report == {"status": "REFUSED_NOT_OPTED_IN"}
    assert "KAKAO" not in completed.stdout
    assert "institution" not in completed.stdout.lower()


# Production break caught: entering a live provider path with one or more runtime
# credentials absent, which could turn an operator mistake into partial traffic.
def test_live_smoke_blocks_missing_runtime_credentials_without_naming_them() -> None:
    completed = _run_smoke({"TRAVEL_MAP_LIVE_SMOKE": "1"})

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {"status": "BLOCKED_MISSING_CREDENTIALS"}
    assert "KAKAO" not in completed.stdout
    assert "OPINET" not in completed.stdout


# Production break caught: following the documented local .env workflow from
# the repository root leaves smoke credentials unread and falsely blocks it.
def test_live_smoke_reads_an_explicit_env_file_without_echoing_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "local.env"
    secret = "never-print-this-runtime-secret"
    env_file.write_text(
        "\n".join(
            (
                f"KAKAO_REST_API_KEY={secret}",
                "SEOUL_TRANSIT_SERVICE_KEY=transit-secret",
                "OPINET_CERT_KEY=opinet-secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    completed = _run_smoke(
        {"TRAVEL_MAP_LIVE_SMOKE": "1"},
        arguments=("--env-file", str(env_file)),
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "status": "BLOCKED_MISSING_APPROVED_SNAPSHOT"
    }
    assert secret not in completed.stdout + completed.stderr


# Production break caught: the sync command's documented env file is ignored,
# causing a present NEIS key to be reported missing or printed in a diagnostic.
def test_sync_reads_an_explicit_env_file_without_echoing_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "sync.env"
    secret = "never-print-this-sync-secret"
    env_file.write_text(f"NEIS_API_KEY={secret}\n", encoding="utf-8")
    environment = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": "apps/travel-map"}

    completed = subprocess.run(
        [
            sys.executable,
            str(SYNC),
            "--env-file",
            str(env_file),
        ],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "NEIS_API_KEY" not in completed.stderr
    assert "KINDERGARTEN_API_KEY" in completed.stderr
    assert "KAKAO_REST_API_KEY" in completed.stderr
    assert secret not in completed.stdout + completed.stderr


# Production break caught: a smoke report retaining route IDs, destination labels,
# coordinates, or allowance amounts instead of the narrowly approved telemetry.
def test_live_case_report_only_emits_approved_operational_fields() -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    report_case = module["_case_report"]
    response = TripPreviewResponse.model_validate(
        {
            "coverage": {"status": "SEOUL"},
            "origin": {
                "siteId": "test-neis:B10:private-origin",
                "name": "sensitive origin",
                "address": "sensitive origin address",
                "coordinate": {"latitude": 37.55, "longitude": 126.98},
            },
            "institutionSnapshotId": "fixture-001",
            "policyScope": "NONPUBLIC_OR_UNKNOWN",
            "classification": "LOCAL",
            "classificationDistanceMeters": 1200,
            "classificationPath": None,
            "routes": [
                {
                    "id": "sensitive-route-id",
                    "mode": "CAR",
                    "durationSeconds": 100,
                    "distanceMeters": 1000,
                    "mobilityCostKrw": 200,
                    "costStatus": "KNOWN",
                    "costBreakdown": None,
                    "geometry": [
                        {"latitude": 37.55, "longitude": 126.98},
                        {"latitude": 37.56, "longitude": 126.99},
                    ],
                    "source": "KAKAO_CAR",
                    "sourceAsOf": "2026-08-10T00:00:00Z",
                    "warnings": [],
                }
            ],
            "best": {
                "fastestRouteId": "sensitive-route-id",
                "shortestRouteId": "sensitive-route-id",
                "cheapestRouteId": "sensitive-route-id",
            },
            "mobilityCost": {"status": "KNOWN", "amountKrw": 200},
            "allowance": {"status": "REVIEW_REQUIRED", "amountKrw": None},
            "ruleSetId": "fixture-rule",
            "effectiveFrom": "2026-08-01",
            "sourceRefs": [],
            "warnings": [],
        }
    )

    report = report_case("NONPUBLIC", response, latency_ms=12)

    assert report == {
        "caseId": "NONPUBLIC",
        "providerStatus": "ROUTES_AVAILABLE",
        "routeCount": 1,
        "decision": "LOCAL",
        "latencyMs": 12,
    }
    assert "sensitive" not in json.dumps(report)


# Production break caught: reporting a generic no-route state after all route
# providers failed, which hides an upstream outage from release review.
@pytest.mark.parametrize(
    ("warning", "expected_status"),
    (
        ("UPSTREAM_UNAVAILABLE", "UPSTREAM_UNAVAILABLE"),
        ("UPSTREAM_RATE_LIMIT", "UPSTREAM_RATE_LIMITED"),
        ("UPSTREAM_REJECTED", "UPSTREAM_REJECTED"),
        ("UPSTREAM_TIMEOUT", "UPSTREAM_TIMEOUT"),
        ("UPSTREAM_ERROR", "UPSTREAM_ERROR"),
        ("SCHEMA_MISMATCH", "RESPONSE_SCHEMA_MISMATCH"),
        ("RESPONSE_TOO_LARGE", "RESPONSE_TOO_LARGE"),
        ("RESPONSE_LIMIT_EXCEEDED", "RESPONSE_LIMIT_EXCEEDED"),
        ("INVALID_PROVIDER_RESULT", "INVALID_PROVIDER_RESPONSE"),
    ),
)
def test_live_case_report_maps_provider_failures_to_safe_statuses(
    warning: str,
    expected_status: str,
) -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    report_case = module["_case_report"]
    response = TripPreviewResponse.model_validate(
        _trip_response_payload(routes=[], warnings=[warning])
    )

    assert report_case("PUBLIC_LOCAL", response, latency_ms=12) == {
        "caseId": "PUBLIC_LOCAL",
        "providerStatus": expected_status,
        "routeCount": 0,
        "decision": "LOCAL",
        "latencyMs": 12,
    }


def _release_source_with_current_snapshot(tmp_path: Path) -> Path:
    source_root = tmp_path / "travel-map"
    shutil.copytree(ROOT, source_root)
    snapshots = source_root / "resources/institution-snapshots"
    snapshots.mkdir()
    shutil.copytree(
        FIXTURE_SNAPSHOT / "fixture-001",
        snapshots / "fixture-001",
    )
    (snapshots / "current.json").write_text(
        json.dumps({"snapshotId": "fixture-001"}),
        encoding="utf-8",
    )
    return source_root


# Production break caught: using a closer headquarters, library, or other
# foundation-matched institution as a smoke origin in a broader snapshot.
def test_live_smoke_selects_a_seoul_active_school_not_any_foundation_match() -> None:
    module = runpy.run_path(str(SMOKE), run_name="release_smoke_test")
    select_origin = module["_nearest_active_school_site"]

    class BroaderStore:
        def search(self, **filters: object) -> tuple[SimpleNamespace, ...]:
            if filters["institution_type"] == "ELEMENTARY_SCHOOL":
                return (SimpleNamespace(site_id="test:school"),)
            return ()

        def require_site(self, site_id: str) -> SimpleNamespace:
            if site_id == "test:school":
                return SimpleNamespace(
                    routing_anchor_latitude=37.57,
                    routing_anchor_longitude=126.98,
                )
            raise AssertionError("non-school institution was selected")

    dependencies = SimpleNamespace(
        institutions=BroaderStore(),
        coverage=SimpleNamespace(classify=lambda _coordinate: CoverageState.SEOUL),
    )

    assert select_origin(dependencies, "PUBLIC") == "test:school"


def _trip_response_payload(
    *,
    routes: list[dict[str, object]],
    warnings: list[str],
) -> dict[str, object]:
    route_id = "sensitive-route-id"
    return {
        "coverage": {"status": "SEOUL"},
        "origin": {
            "siteId": "test-neis:B10:private-origin",
            "name": "sensitive origin",
            "address": "sensitive origin address",
            "coordinate": {"latitude": 37.55, "longitude": 126.98},
        },
        "institutionSnapshotId": "fixture-001",
        "policyScope": "NONPUBLIC_OR_UNKNOWN",
        "classification": "LOCAL",
        "classificationDistanceMeters": 1200,
        "classificationPath": None,
        "routes": routes,
        "best": {
            "fastestRouteId": route_id if routes else None,
            "shortestRouteId": route_id if routes else None,
            "cheapestRouteId": route_id if routes else None,
        },
        "mobilityCost": {"status": "KNOWN", "amountKrw": 200},
        "allowance": {"status": "REVIEW_REQUIRED", "amountKrw": None},
        "ruleSetId": "fixture-rule",
        "effectiveFrom": "2026-08-01",
        "sourceRefs": [],
        "warnings": warnings,
    }


# Production break caught: a container context including credentials, raw inputs,
# or test-only resources even when Docker is unavailable for an integration build.
def test_release_container_artifacts_exclude_non_runtime_payloads() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for forbidden in (
        ".env",
        ".git",
        "tests/",
        "e2e/",
        "resources/geodata/source/",
        "resources/institution-sources/",
        "artifacts/",
    ):
        assert forbidden in dockerignore
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "resources/rules" in dockerfile
    assert "resources/geodata/seoul.geojson" in dockerfile
    assert "resources/geodata/seoul-plus-12km.geojson" in dockerfile
    assert "resources/institution-snapshots" in dockerfile


def _run_smoke(
    extra_environment: dict[str, str],
    *,
    arguments: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    for name in (
        "TRAVEL_MAP_LIVE_SMOKE",
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
    ):
        environment.pop(name, None)
    environment.update(extra_environment)
    return subprocess.run(
        [sys.executable, str(SMOKE.resolve()), *arguments],
        cwd=Path.cwd(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
