from dataclasses import dataclass, field

from app.routing.models import (
    ProviderResult,
    ProviderWarning,
    RouteQuery,
    TravelMode,
)
from app.routing.provider import RouteProvider
from app.settings import Settings


@dataclass
class SeoulTransitProvider:
    name: str = field(default="SEOUL_TRANSIT", init=False)
    supported_modes: frozenset[TravelMode] = field(
        default=frozenset({TravelMode.TRANSIT}),
        init=False,
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> "SeoulTransitProvider":
        del settings
        return cls()

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        return _not_implemented(self.name)


@dataclass
class KakaoTransitProvider:
    name: str = field(default="KAKAO_TRANSIT", init=False)
    supported_modes: frozenset[TravelMode] = field(
        default=frozenset({TravelMode.TRANSIT}),
        init=False,
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> "KakaoTransitProvider":
        del settings
        return cls()

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        return _not_implemented(self.name)


@dataclass
class KakaoWalkProvider:
    name: str = field(default="KAKAO_WALK", init=False)
    supported_modes: frozenset[TravelMode] = field(
        default=frozenset({TravelMode.WALK}),
        init=False,
    )

    @classmethod
    def from_settings(cls, settings: Settings) -> "KakaoWalkProvider":
        del settings
        return cls()

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        return _not_implemented(self.name)


@dataclass
class KakaoCarProvider:
    priority: str = "RECOMMEND"
    alternatives: bool = True
    name: str = field(default="KAKAO_CAR", init=False)
    supported_modes: frozenset[TravelMode] = field(
        default=frozenset({TravelMode.CAR}),
        init=False,
    )

    def __post_init__(self) -> None:
        if self.priority not in {"RECOMMEND", "DISTANCE"}:
            raise ValueError("unsupported Kakao car priority")
        if type(self.alternatives) is not bool:
            raise TypeError("alternatives must be bool")

    @classmethod
    def from_settings(cls, settings: Settings) -> "KakaoCarProvider":
        del settings
        return cls()

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        return _not_implemented(self.name)


def build_car_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoCarProvider.from_settings(settings),)


def build_walk_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoWalkProvider.from_settings(settings),)


def build_route_providers(
    settings: Settings,
) -> dict[TravelMode, tuple[RouteProvider, ...]]:
    seoul_transit = SeoulTransitProvider.from_settings(settings)
    kakao_transit = KakaoTransitProvider.from_settings(settings)
    return {
        TravelMode.TRANSIT: (seoul_transit, kakao_transit),
        TravelMode.CAR: build_car_provider_chain(settings),
        TravelMode.WALK: build_walk_provider_chain(settings),
    }


def build_classification_provider(settings: Settings) -> RouteProvider:
    del settings
    return KakaoCarProvider(priority="DISTANCE", alternatives=False)


def _not_implemented(provider: str) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        routes=(),
        warnings=(
            ProviderWarning(
                code="PROVIDER_NOT_IMPLEMENTED",
                message="Concrete route adapter is not installed in Stage A",
                source=provider,
            ),
        ),
    )
