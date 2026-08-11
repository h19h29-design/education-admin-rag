import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import replace
from math import isfinite

from app.routing.models import (
    ProviderResult,
    ProviderWarning,
    RouteCollection,
    RouteOption,
    RouteQuery,
    TravelMode,
)
from app.routing.provider import RouteProvider
from app.routing.ranking import deduplicate_routes, rank_routes


class RouteOrchestrator:
    def __init__(
        self,
        providers: Mapping[TravelMode, tuple[RouteProvider, ...]],
        *,
        max_concurrency: int,
        provider_timeout_seconds: float = 5.0,
    ) -> None:
        if type(max_concurrency) is not int or max_concurrency <= 0:
            raise ValueError("max_concurrency must be a positive integer")
        if (
            type(provider_timeout_seconds) is not float
            or not isfinite(provider_timeout_seconds)
            or provider_timeout_seconds <= 0
        ):
            raise ValueError("provider_timeout_seconds must be a positive finite float")
        normalized: dict[TravelMode, tuple[RouteProvider, ...]] = {}
        for mode, chain in providers.items():
            if type(mode) is not TravelMode:
                raise TypeError("provider registry keys must be TravelMode")
            if type(chain) is not tuple:
                raise TypeError("provider chains must be tuples")
            for provider in chain:
                _validate_provider(provider)
            normalized[mode] = chain
        self._providers = normalized
        self._provider_timeout_seconds = provider_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def collect(
        self,
        query_base: RouteQuery,
        requested_modes: Iterable[TravelMode],
    ) -> RouteCollection:
        if type(query_base) is not RouteQuery:
            raise TypeError("query_base must be RouteQuery")
        requested = set(requested_modes)
        if any(type(mode) is not TravelMode for mode in requested):
            raise TypeError("requested_modes must contain TravelMode values")
        ordered_modes = tuple(mode for mode in TravelMode if mode in requested)
        outcomes = await asyncio.gather(
            *(self._collect_mode(query_base, mode) for mode in ordered_modes)
        )

        routes: list[RouteOption] = []
        warnings: list[ProviderWarning] = []
        priorities: dict[str, int] = {}
        priority = 0
        for mode in ordered_modes:
            for provider in self._providers.get(mode, ()):
                priorities.setdefault(provider.name, priority)
                priority += 1
        for mode_routes, mode_warnings in outcomes:
            routes.extend(mode_routes)
            warnings.extend(mode_warnings)

        unique_routes, duplicate_warnings = _drop_duplicate_route_ids(routes)
        warnings.extend(duplicate_warnings)
        normalized_routes = deduplicate_routes(
            unique_routes,
            provider_priorities=priorities,
        )
        return RouteCollection(
            routes=normalized_routes,
            best=rank_routes(
                normalized_routes,
                provider_priorities=priorities,
            ),
            warnings=tuple(warnings),
        )

    async def _collect_mode(
        self,
        query_base: RouteQuery,
        mode: TravelMode,
    ) -> tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]:
        query = replace(
            query_base,
            mode=mode,
            car_assumptions=(
                query_base.car_assumptions if mode is TravelMode.CAR else None
            ),
        )
        chain = self._providers.get(mode, ())
        if not chain:
            warning = ProviderWarning(
                code="NO_PROVIDER",
                message=f"No provider is configured for {mode.value}",
                source="ROUTE_ORCHESTRATOR",
            )
            return (), (warning,)

        warnings: list[ProviderWarning] = []
        for provider in chain:
            if mode not in provider.supported_modes:
                warnings.append(
                    ProviderWarning(
                        code="CAPABILITY_MISSING",
                        message=f"Provider does not support {mode.value}",
                        source=provider.name,
                    )
                )
                continue
            try:
                async with self._semaphore:
                    result = await asyncio.wait_for(
                        provider.get_routes(query),
                        timeout=self._provider_timeout_seconds,
                    )
            except TimeoutError:
                warnings.append(
                    ProviderWarning(
                        code="UPSTREAM_TIMEOUT",
                        message="Route provider timed out",
                        source=provider.name,
                    )
                )
                continue
            except Exception:  # noqa: BLE001
                warnings.append(
                    ProviderWarning(
                        code="UPSTREAM_ERROR",
                        message="Route provider request failed",
                        source=provider.name,
                    )
                )
                continue

            if type(result) is not ProviderResult:
                warnings.append(
                    ProviderWarning(
                        code="INVALID_PROVIDER_RESULT",
                        message="Route provider returned an invalid result",
                        source=provider.name,
                    )
                )
                continue
            if result.provider != provider.name:
                warnings.append(
                    ProviderWarning(
                        code="PROVIDER_IDENTITY_MISMATCH",
                        message="Route provider identity did not match its registry entry",
                        source=provider.name,
                    )
                )
                continue
            warnings.extend(result.warnings)
            if any(route.mode is not mode for route in result.routes):
                warnings.append(
                    ProviderWarning(
                        code="MODE_MISMATCH",
                        message="Route provider returned a different travel mode",
                        source=provider.name,
                    )
                )
                continue
            if result.routes:
                return result.routes, tuple(warnings)
            if not result.warnings:
                warnings.append(
                    ProviderWarning(
                        code="NO_ROUTES",
                        message="Route provider returned no routes",
                        source=provider.name,
                    )
                )
        return (), tuple(warnings)


def _drop_duplicate_route_ids(
    routes: list[RouteOption],
) -> tuple[tuple[RouteOption, ...], tuple[ProviderWarning, ...]]:
    seen: set[str] = set()
    kept: list[RouteOption] = []
    warnings: list[ProviderWarning] = []
    for route in routes:
        if route.id in seen:
            warnings.append(
                ProviderWarning(
                    code="DUPLICATE_ROUTE_ID",
                    message="A duplicate route id was excluded",
                    source=route.source,
                )
            )
            continue
        seen.add(route.id)
        kept.append(route)
    return tuple(kept), tuple(warnings)


def _validate_provider(provider: object) -> None:
    name = getattr(provider, "name", None)
    if type(name) is not str:
        raise TypeError("provider name must be str")
    if not name.strip():
        raise ValueError("provider name must be nonblank")
    supported_modes = getattr(provider, "supported_modes", None)
    if type(supported_modes) is not frozenset:
        raise TypeError("provider supported_modes must be frozenset")
    if any(type(mode) is not TravelMode for mode in supported_modes):
        raise TypeError("provider supported_modes must contain TravelMode values")
    if not callable(getattr(provider, "get_routes", None)):
        raise TypeError("provider get_routes must be callable")
