import pytest
from app.routing.bootstrap import (
    KakaoCarProvider,
    build_car_provider_chain,
    build_classification_provider,
    build_route_providers,
)
from app.routing.models import TravelMode
from app.settings import Settings
from tests.routing.fakes import FakeProvider, base_query, result_with, route


# Break caught: a Stage A provider order regression changing fallback priority.
def test_stage_a_provider_order_is_explicit() -> None:
    settings = Settings()

    providers = build_route_providers(settings)

    assert [provider.name for provider in providers[TravelMode.TRANSIT]] == [
        "SEOUL_TRANSIT",
        "KAKAO_TRANSIT",
    ]
    assert [provider.name for provider in providers[TravelMode.CAR]] == ["KAKAO_CAR"]
    assert [provider.name for provider in providers[TravelMode.WALK]] == ["KAKAO_WALK"]


# Break caught: a car-engine extension replacing the independent walk chain.
def test_car_and_walk_extension_points_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    monkeypatch.setattr(
        "app.routing.bootstrap.build_car_provider_chain",
        lambda _settings: (
            FakeProvider(
                "PUBLIC_CAR",
                result_with(route("public-car", 600, 4_000, 1_000)),
            ),
        ),
    )

    providers = build_route_providers(settings)

    assert [provider.name for provider in providers[TravelMode.CAR]] == ["PUBLIC_CAR"]
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
