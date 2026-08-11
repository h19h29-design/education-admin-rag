import math

import pytest
from app.settings import Settings
from pydantic import SecretStr, ValidationError
from pydantic_settings import SettingsError


def production_values() -> dict[str, object]:
    return {
        "environment": "production",
        "kakao_rest_api_key": SecretStr("kakao-secret"),
        "seoul_transit_service_key": SecretStr("seoul-secret"),
        "opinet_cert_key": SecretStr("opinet-secret"),
        "allowed_hosts": ("travel.example.kr",),
        "allowed_origins": ("https://travel.example.kr",),
        "provider_timeout_seconds": 4.0,
        "route_max_concurrency": 4,
    }


# Break caught: starting the public production server without required upstreams.
def test_production_settings_require_all_provider_keys_hosts_and_origins() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", _env_file=None)

    settings = Settings(**production_values(), _env_file=None)

    assert settings.environment == "production"
    assert "kakao-secret" not in repr(settings)
    assert "seoul-secret" not in repr(settings)
    assert "opinet-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", True),
        ("provider_timeout_seconds", math.inf),
        ("provider_timeout_seconds", 0.0),
        ("route_max_concurrency", True),
        ("route_max_concurrency", 0),
        ("allowed_hosts", ["travel.example.kr"]),
        ("allowed_hosts", ("*",)),
        ("allowed_origins", ("*",)),
        ("allowed_origins", ("http://travel.example.kr",)),
    ],
)
def test_production_settings_reject_noncanonical_or_unsafe_values(
    field: str,
    value: object,
) -> None:
    values = production_values()
    values[field] = value

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_development_settings_allow_missing_credentials_but_not_bad_limits() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "development"
    assert settings.kakao_rest_api_key is None
    assert settings.seoul_transit_service_key is None
    assert settings.opinet_cert_key is None
    with pytest.raises(ValidationError):
        Settings(provider_timeout_seconds=float("nan"), _env_file=None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_timeout_seconds", 5),
        ("route_max_concurrency", 4.0),
        ("allowed_hosts", ("localhost",)),
        ("allowed_origins", ("https://travel.example.kr/",)),
    ],
)
def test_settings_reject_wrong_exact_types_and_noncanonical_origin(
    field: str,
    value: object,
) -> None:
    if field == "allowed_hosts":

        class TupleSubclass(tuple[str, ...]):
            pass

        value = TupleSubclass(value)
    values: dict[str, object] = {field: value}
    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


def test_create_app_fails_startup_for_invalid_production_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in (
        "KAKAO_REST_API_KEY",
        "SEOUL_TRANSIT_SERVICE_KEY",
        "OPINET_CERT_KEY",
        "ALLOWED_HOSTS",
        "ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        create_app()


def test_environment_json_lists_become_exact_host_and_origin_tuples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_HOSTS", '["travel.example.kr"]')
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://travel.example.kr"]')

    settings = Settings(_env_file=None)

    assert type(settings.allowed_hosts) is tuple
    assert settings.allowed_hosts == ("travel.example.kr",)
    assert type(settings.allowed_origins) is tuple
    assert settings.allowed_origins == ("https://travel.example.kr",)


def test_production_rejects_empty_environment_host_instead_of_using_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("KAKAO_REST_API_KEY", "kakao")
    monkeypatch.setenv("SEOUL_TRANSIT_SERVICE_KEY", "seoul")
    monkeypatch.setenv("OPINET_CERT_KEY", "opinet")
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    monkeypatch.setenv("ALLOWED_ORIGINS", '["https://travel.example.kr"]')

    with pytest.raises((SettingsError, ValidationError)):
        Settings(_env_file=None)


def test_production_requires_explicit_host_and_origin_allowlists() -> None:
    values = production_values()
    values.pop("allowed_hosts")

    with pytest.raises(ValidationError):
        Settings(**values, _env_file=None)


@pytest.mark.parametrize(
    "host",
    ("*.example.kr", "Travel.Example.Kr", "example.kr:bad"),
)
def test_allowed_hosts_are_exact_canonical_names(host: str) -> None:
    with pytest.raises(ValidationError):
        Settings(allowed_hosts=(host,), _env_file=None)
