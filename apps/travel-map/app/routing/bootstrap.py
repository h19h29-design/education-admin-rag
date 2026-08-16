from app.providers.kakao_map import KakaoTransitProvider, KakaoWalkProvider
from app.providers.kakao_mobility import KakaoCarProvider
from app.providers.seoul_transit import SeoulTransitProvider
from app.routing.models import TravelMode
from app.routing.provider import RouteProvider
from app.settings import Settings


def build_car_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoCarProvider.from_settings(settings),)


def build_walk_provider_chain(settings: Settings) -> tuple[RouteProvider, ...]:
    return (KakaoWalkProvider.from_settings(settings),)


def build_route_providers(
    settings: Settings,
) -> dict[TravelMode, tuple[RouteProvider, ...]]:
    if type(settings) is not Settings:
        raise TypeError("settings must be an exact Settings")
    return {
        TravelMode.TRANSIT: (
            SeoulTransitProvider.from_settings(settings),
            KakaoTransitProvider.from_settings(settings),
        ),
        TravelMode.CAR: build_car_provider_chain(settings),
        TravelMode.WALK: build_walk_provider_chain(settings),
    }


def build_classification_provider(settings: Settings) -> RouteProvider:
    return KakaoCarProvider.from_settings(
        settings,
        priority="DISTANCE",
        alternatives=False,
    )
