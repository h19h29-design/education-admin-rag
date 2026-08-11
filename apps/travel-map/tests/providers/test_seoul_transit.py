import httpx
import pytest
from app.providers.seoul_transit import SeoulTransitProvider
from app.routing.models import CostStatus, TravelMode
from pydantic import SecretStr
from tests.providers.helpers import FIXTURES, NOW, route_query


@pytest.mark.asyncio
async def test_seoul_transit_normalizes_minutes_and_reports_missing_capabilities() -> (
    None
):
    secret = "seoul+service/key?secret"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["serviceKey"] == secret
        assert request.url.params["startX"] == "126.97"
        assert request.url.params["startY"] == "37.55"
        assert request.url.params["endX"] == "126.98"
        assert request.url.params["endY"] == "37.56"
        assert request.url.params["resultType"] == "xml"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml;charset=UTF-8"},
            content=(FIXTURES / "seoul-transit.xml").read_bytes(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SeoulTransitProvider(
            http=http,
            service_key=SecretStr(secret),
            now=lambda: NOW,
        )
        result = await provider.get_routes(route_query(TravelMode.TRANSIT))

    assert len(result.routes) == 1
    route = result.routes[0]
    assert route.duration_seconds == 47 * 60
    assert route.distance_meters == 14600
    assert route.cost_status is CostStatus.UNKNOWN
    assert route.geometry == (
        route_query(TravelMode.TRANSIT).origin,
        route_query(TravelMode.TRANSIT).destination,
    )
    assert route.warnings == ("GEOMETRY_MISSING", "FARE_MISSING")
    assert [warning.code for warning in result.warnings] == [
        "GEOMETRY_MISSING",
        "FARE_MISSING",
    ]
    assert secret not in repr(result)
    assert secret not in repr(provider)


@pytest.mark.asyncio
async def test_seoul_transit_missing_key_and_wrong_mode_do_not_call_network() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        missing = SeoulTransitProvider(http=http, service_key=None)
        wrong = SeoulTransitProvider(http=http, service_key=SecretStr("key"))
        missing_result = await missing.get_routes(route_query(TravelMode.TRANSIT))
        wrong_result = await wrong.get_routes(route_query(TravelMode.CAR))

    assert [warning.code for warning in missing_result.warnings] == [
        "MISSING_CREDENTIAL"
    ]
    assert [warning.code for warning in wrong_result.warnings] == ["UNSUPPORTED_MODE"]
    assert requests == 0


@pytest.mark.asyncio
async def test_seoul_transit_rejects_doctype_bad_header_and_route_limit() -> None:
    bodies = [
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]><ServiceResult />',
        b"<ServiceResult><msgHeader><headerCd>1</headerCd><headerMsg>secret detail</headerMsg><itemCount>0</itemCount></msgHeader><msgBody /></ServiceResult>",
        (
            b"<ServiceResult><msgHeader><headerCd>0</headerCd><headerMsg/><itemCount>2</itemCount></msgHeader><msgBody>"
            + b"<itemList><distance>1</distance><time>1</time><pathList><routeId>1</routeId></pathList></itemList>"
            * 2
            + b"</msgBody></ServiceResult>"
        ),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/xml"},
            content=bodies.pop(0),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SeoulTransitProvider(
            http=http,
            service_key=SecretStr("key"),
            max_routes=1,
        )
        results = [
            await provider.get_routes(route_query(TravelMode.TRANSIT)) for _ in range(3)
        ]

    assert [result.warnings[0].code for result in results] == [
        "SCHEMA_MISMATCH",
        "UPSTREAM_REJECTED",
        "RESPONSE_LIMIT_EXCEEDED",
    ]
    assert "secret detail" not in repr(results[1])


@pytest.mark.asyncio
async def test_seoul_transit_rejects_dtd_and_entity_after_large_prefix() -> None:
    fixture = (FIXTURES / "seoul-transit.xml").read_bytes()
    declaration, document = fixture.split(b"?>", 1)
    raw = (
        declaration
        + b"?>"
        + b" " * 5_000
        + b'<!DOCTYPE ServiceResult [<!ENTITY injected "expanded">]>'
        + document.replace(
            "정상적으로 처리되었습니다.".encode(),
            b"&injected;",
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            content=raw,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await SeoulTransitProvider(
            http=http,
            service_key=SecretStr("key"),
        ).get_routes(route_query(TravelMode.TRANSIT))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["SCHEMA_MISMATCH"]


# Break caught: a byte-pattern scan cannot see DTD/entity tokens encoded as UTF-16.
@pytest.mark.asyncio
async def test_seoul_transit_rejects_utf16_encoded_dtd_and_entity() -> None:
    raw = """<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE ServiceResult [<!ENTITY injected "expanded">]>
<ServiceResult><msgHeader><headerCd>0</headerCd><headerMsg>&injected;</headerMsg>
<itemCount>0</itemCount></msgHeader><msgBody /></ServiceResult>""".encode("utf-16")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            content=raw,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await SeoulTransitProvider(
            http=http,
            service_key=SecretStr("key"),
        ).get_routes(route_query(TravelMode.TRANSIT))

    assert result.routes == ()
    assert [warning.code for warning in result.warnings] == ["SCHEMA_MISMATCH"]


@pytest.mark.asyncio
async def test_seoul_transit_rejects_oversized_response_without_parsing() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            content=b"x" * 101,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        provider = SeoulTransitProvider(
            http=http,
            service_key=SecretStr("key"),
            max_response_bytes=100,
        )
        result = await provider.get_routes(route_query(TravelMode.TRANSIT))

    assert [warning.code for warning in result.warnings] == ["RESPONSE_TOO_LARGE"]


@pytest.mark.asyncio
async def test_seoul_transit_validates_documented_path_coordinates() -> None:
    raw = (
        (FIXTURES / "seoul-transit.xml")
        .read_bytes()
        .replace(b"<fx>126.9700</fx>", b"<fx>NaN</fx>")
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/xml"},
            content=raw,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await SeoulTransitProvider(
            http=http,
            service_key=SecretStr("key"),
        ).get_routes(route_query(TravelMode.TRANSIT))

    assert [warning.code for warning in result.warnings] == ["SCHEMA_MISMATCH"]
