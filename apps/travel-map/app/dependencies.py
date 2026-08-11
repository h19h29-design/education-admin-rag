"""Application-owned dependencies and production fail-closed assembly."""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import orjson

from app.cache import TtlLruCache
from app.institutions.store import InstitutionStore
from app.policy.coverage import CoverageService
from app.policy.engine import PolicyEngine
from app.policy.rules import RuleRepository
from app.providers.kakao_local import KakaoLocalClient
from app.rate_limit import FixedWindowRateLimiter
from app.routing.bootstrap import build_classification_provider, build_route_providers
from app.routing.orchestrator import RouteOrchestrator
from app.routing.provider import RouteProvider
from app.settings import Settings


class PlaceClient(Protocol):
    async def aclose(self) -> None:
        raise NotImplementedError


@dataclass
class AppDependencies:
    settings: Settings
    institutions: InstitutionStore
    coverage: CoverageService
    policy: PolicyEngine
    route_orchestrator: RouteOrchestrator
    classification_provider: RouteProvider
    place_client: PlaceClient
    cache: TtlLruCache
    rate_limiter: FixedWindowRateLimiter
    seoul_geojson: bytes
    support_geojson: bytes
    _closed_resource_ids: set[int] = field(default_factory=set, init=False)

    async def aclose(self) -> None:
        """Close each injectable external resource once, even after one failure."""

        failures: list[Exception] = []
        for resource in self._resources_to_close():
            identifier = id(resource)
            if identifier in self._closed_resource_ids:
                continue
            close = getattr(resource, "aclose", None)
            if not callable(close):
                continue
            self._closed_resource_ids.add(identifier)
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - cleanup reaches later resources
                failures.append(exc)
        if failures:
            raise failures[0]

    def _resources_to_close(self) -> Iterable[object]:
        providers = getattr(self.route_orchestrator, "_providers", {})
        if isinstance(providers, dict):
            for chain in providers.values():
                if isinstance(chain, tuple):
                    yield from chain
        yield self.classification_provider
        yield self.place_client


def build_production_dependencies(settings: Settings) -> AppDependencies:
    """Load only verified local artifacts; missing artifacts prevent production start."""

    if type(settings) is not Settings:
        raise TypeError("settings must be an exact Settings")
    root = Path(__file__).resolve().parents[1]
    resources = root / "resources"
    snapshot_root = resources / "institution-snapshots"
    if not snapshot_root.is_dir():
        raise RuntimeError("verified institution snapshot is unavailable")
    seoul_path = resources / "geodata/seoul.geojson"
    support_path = resources / "geodata/seoul-plus-12km.geojson"
    seoul_geojson = _verified_geojson_bytes(seoul_path)
    support_geojson = _verified_geojson_bytes(support_path)
    route_providers = build_route_providers(settings)
    return AppDependencies(
        settings=settings,
        institutions=InstitutionStore.load(snapshot_root),
        coverage=CoverageService.from_geojson(
            seoul_path=seoul_path,
            buffer_distance_m=12_000,
        ),
        policy=PolicyEngine(RuleRepository.from_directory(resources / "rules")),
        route_orchestrator=RouteOrchestrator(
            route_providers,
            max_concurrency=settings.route_max_concurrency,
            provider_timeout_seconds=settings.provider_timeout_seconds,
        ),
        classification_provider=build_classification_provider(settings),
        place_client=KakaoLocalClient(
            rest_key=settings.kakao_rest_api_key,
            max_response_bytes=1_000_000,
        ),
        cache=TtlLruCache(max_entries=2_000),
        rate_limiter=FixedWindowRateLimiter(
            limits={"places": (10, 60.0), "preview": (20, 60.0)}
        ),
        seoul_geojson=seoul_geojson,
        support_geojson=support_geojson,
    )


def _verified_geojson_bytes(path: Path) -> bytes:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"verified geodata is unavailable: {path.name}") from exc
    if (
        type(payload) is not dict
        or payload.get("type") != "FeatureCollection"
        or type(payload.get("features")) is not list
        or not payload["features"]
        or any(type(feature) is not dict for feature in payload["features"])
    ):
        raise RuntimeError(f"verified geodata is invalid: {path.name}")
    return orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
