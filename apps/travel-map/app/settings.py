import re
from math import isfinite
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


class _TupleNormalizingSettingsSource(PydanticBaseSettingsSource):
    """Normalize JSON arrays only at environment-backed settings boundaries."""

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        source: PydanticBaseSettingsSource,
    ) -> None:
        super().__init__(settings_cls)
        self._source = source

    def get_field_value(
        self,
        field: FieldInfo,
        field_name: str,
    ) -> tuple[Any, str, bool]:
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self._source()
        for name in (
            "kakao_rest_api_key",
            "seoul_transit_service_key",
            "opinet_cert_key",
        ):
            value = values.get(name)
            if value == "" or (
                type(value) is SecretStr and value.get_secret_value() == ""
            ):
                values[name] = None
        for name in ("allowed_hosts", "allowed_origins"):
            value = values.get(name)
            if type(value) is list:
                values[name] = tuple(value)
        return values


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        strict=True,
        env_ignore_empty=False,
    )

    environment: Literal["development", "test", "production"] = "development"
    kakao_rest_api_key: SecretStr | None = None
    seoul_transit_service_key: SecretStr | None = None
    opinet_cert_key: SecretStr | None = None
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    )
    provider_timeout_seconds: float = 5.0
    route_max_concurrency: int = 4

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _TupleNormalizingSettingsSource(settings_cls, env_settings),
            _TupleNormalizingSettingsSource(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    @field_validator("environment", mode="before")
    @classmethod
    def validate_environment_type(cls, value: object) -> object:
        if type(value) is not str:
            raise ValueError("environment must be an exact string")
        return value

    @field_validator(
        "kakao_rest_api_key",
        "seoul_transit_service_key",
        "opinet_cert_key",
    )
    @classmethod
    def reject_blank_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("provider credentials must be nonblank")
        return value

    @field_validator("provider_timeout_seconds", mode="before")
    @classmethod
    def validate_timeout(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("provider timeout must be an exact float")
        if not isfinite(value) or not 0.0 < value <= 30.0:
            raise ValueError("provider timeout must be finite and in (0, 30]")
        return value

    @field_validator("route_max_concurrency", mode="before")
    @classmethod
    def validate_concurrency(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("route concurrency must be an exact int")
        if not 1 <= value <= 32:
            raise ValueError("route concurrency must be in [1, 32]")
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def validate_hosts(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("allowed_hosts must be an exact tuple")
        if not value:
            raise ValueError("allowed_hosts must not be empty")
        for host in value:
            if type(host) is not str or not host.strip() or host != host.strip():
                raise ValueError("allowed hosts must be canonical strings")
            if not _is_canonical_host(host):
                raise ValueError("allowed hosts must be exact host names")
        if len(value) != len(set(value)):
            raise ValueError("allowed hosts must be unique")
        return value

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def validate_origins(cls, value: object) -> object:
        if type(value) is not tuple:
            raise ValueError("allowed_origins must be an exact tuple")
        if not value:
            raise ValueError("allowed_origins must not be empty")
        for origin in value:
            if (
                type(origin) is not str
                or not origin.strip()
                or origin != origin.strip()
            ):
                raise ValueError("allowed origins must be canonical strings")
            parsed = urlsplit(origin)
            try:
                port = parsed.port
            except ValueError:
                raise ValueError(
                    "allowed origins must use a valid numeric port"
                ) from None
            hostname = parsed.hostname
            canonical_netloc = (
                f"{hostname}:{port}"
                if hostname is not None and port is not None
                else hostname
            )
            if (
                origin == "*"
                or parsed.scheme not in {"http", "https"}
                or hostname is None
                or not _is_canonical_host(hostname)
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != ""
                or parsed.query
                or parsed.fragment
                or parsed.netloc != canonical_netloc
                or origin != f"{parsed.scheme}://{canonical_netloc}"
                or (parsed.scheme == "http" and port == 80)
                or (parsed.scheme == "https" and port == 443)
            ):
                raise ValueError("allowed origins must be exact HTTP origins")
        if len(value) != len(set(value)):
            raise ValueError("allowed origins must be unique")
        return value

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.environment != "production":
            return self
        if not {"allowed_hosts", "allowed_origins"}.issubset(self.model_fields_set):
            raise ValueError("production host and origin allowlists must be explicit")
        missing = tuple(
            name
            for name, value in (
                ("KAKAO_REST_API_KEY", self.kakao_rest_api_key),
                ("SEOUL_TRANSIT_SERVICE_KEY", self.seoul_transit_service_key),
                ("OPINET_CERT_KEY", self.opinet_cert_key),
            )
            if value is None
        )
        if missing:
            raise ValueError("production provider credentials are incomplete")
        if any(urlsplit(origin).scheme != "https" for origin in self.allowed_origins):
            raise ValueError("production origins must use HTTPS")
        return self


def _is_canonical_host(value: str) -> bool:
    if (
        type(value) is not str
        or value != value.lower()
        or len(value) > 253
        or value.startswith(".")
        or value.endswith(".")
        or ":" in value
    ):
        return False
    labels = value.split(".")
    return all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is not None
        for label in labels
    )
