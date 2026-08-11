import pytest
from app.routing.bootstrap import (
    KakaoCarProvider,
    build_car_provider_chain,
    build_classification_provider,
    build_route_providers,
    build_walk_provider_chain,
)
from app.routing.models import TravelMode
from app.settings import Settings
from tests.routing.fakes import base_query


# Break caught: stage B/C helpers overwriting each other's provider chain.
def test_bootstrap_builds_independent_mode_chains_in_fixed_order() -> None:
    settings = Settings()

    providers = build_route_providers(settings)

    assert [provider.name for provider in providers[TravelMode.TRANSIT]] == [
        "SEOUL_TRANSIT",
        "KAKAO_TRANSIT",
    ]
    assert providers[TravelMode.CAR] == build_car_provider_chain(settings)
    assert providers[TravelMode.WALK] == build_walk_provider_chain(settings)
    assert [provider.name for provider in providers[TravelMode.CAR]] == ["KAKAO_CAR"]
    assert [provider.name for provider in providers[TravelMode.WALK]] == ["KAKAO_WALK"]


# Break caught: legal classification reusing recommendation/alternative routes.
def test_classification_provider_is_separate_distance_only_car_instance() -> None:
    settings = Settings()

    display = build_car_provider_chain(settings)[0]
    classification = build_classification_provider(settings)

    assert isinstance(classification, KakaoCarProvider)
    assert classification is not display
    assert classification.priority == "DISTANCE"
    assert classification.alternatives is False
    assert display.priority == "RECOMMEND"
    assert display.alternatives is True


# Break caught: development without keys making a network request or fake success.
@pytest.mark.asyncio
async def test_provider_factories_fail_closed_without_credentials() -> None:
    provider = build_route_providers(Settings())[TravelMode.TRANSIT][0]

    result = await provider.get_routes(base_query())

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["MISSING_CREDENTIAL"]
