import httpx
import pytest
from app.providers.kakao_local import BoundingBox, KakaoLocalClient, PlaceCandidate
from app.routing.models import Coordinate
from pydantic import SecretStr
from tests.providers.helpers import load_json


class _ChunkedBody(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield b"x" * 101

    async def aclose(self) -> None:
        return None


def test_place_candidate_and_bounds_reject_wrong_types_nonfinite_and_order() -> None:
    with pytest.raises(TypeError):
        BoundingBox(west=True, south=37.4, east=127.3, north=37.75)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BoundingBox(west=126.7, south=37.4, east=126.7, north=37.75)
    with pytest.raises(ValueError):
        BoundingBox(west=126.7, south=float("nan"), east=127.3, north=37.75)
    with pytest.raises(TypeError):
        PlaceCandidate("id", "name", "road", "lot", 37, 127.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_place_search_uses_rect_encoding_limits_results_and_keeps_key_off_url() -> (
    None
):
    secret = "local-header-secret"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == f"KakaoAK {secret}"
        assert request.url.params["query"] == "서울 시청/도서관"
        assert request.url.params["rect"] == "126.7,37.4,127.3,37.75"
        assert request.url.params["page"] == "1"
        assert request.url.params["size"] == "15"
        assert secret not in str(request.url)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json;charset=UTF-8"},
            json=load_json("kakao-keyword.json"),
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = KakaoLocalClient(http=http, rest_key=SecretStr(secret))
    bounds = BoundingBox(west=126.7, south=37.4, east=127.3, north=37.75)

    places = await client.search("서울 시청/도서관", bounds=bounds)
    await client.aclose()

    assert [place.place_id for place in places] == ["kakao-place-1", "kakao-place-2"]
    assert places[0].latitude == 37.56661
    assert len(places) <= 15
    assert len(seen) == 1
    assert not http.is_closed
    await http.aclose()


@pytest.mark.asyncio
async def test_place_search_rejects_blank_short_and_long_without_request() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        bounds = BoundingBox(west=126.7, south=37.4, east=127.3, north=37.75)
        assert await client.search("   ", bounds=bounds) == ()
        assert await client.search("가", bounds=bounds) == ()
        assert await client.search("가" * 81, bounds=bounds) == ()

    assert requests == 0


@pytest.mark.asyncio
async def test_reverse_geocode_prefers_road_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["x"] == "126.9779"
        assert request.url.params["y"] == "37.5663"
        assert request.url.params["input_coord"] == "WGS84"
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json=load_json("kakao-coord2address.json"),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr("test-key"))
        place = await client.reverse_geocode(Coordinate(37.5663, 126.9779))

    assert place is not None
    assert place.name == "서울특별시청"
    assert place.road_address == "서울 중구 세종대로 110"
    assert place.lot_address == "서울 중구 태평로1가 31"


@pytest.mark.asyncio
async def test_local_http_errors_are_bounded_deterministic_and_secret_free() -> None:
    secret = "never-leak-local"
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            429,
            headers={"Content-Type": "application/json"},
            json={"secretEcho": secret},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(http=http, rest_key=SecretStr(secret))
        result = await client.search(
            "서울시청",
            bounds=BoundingBox(126.7, 37.4, 127.3, 37.75),
        )

    assert result == ()
    assert client.last_warnings == ("UPSTREAM_RATE_LIMIT",)
    assert requests == 2
    assert secret not in repr(client)
    assert secret not in repr(client.last_warnings)


@pytest.mark.asyncio
async def test_local_rejects_wrong_content_type_and_oversized_body() -> None:
    responses = iter(
        (
            httpx.Response(200, headers={"Content-Type": "text/html"}, text="{}"),
            httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b"x" * 101,
            ),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(
            http=http,
            rest_key=SecretStr("test-key"),
            max_response_bytes=100,
        )
        bounds = BoundingBox(126.7, 37.4, 127.3, 37.75)
        assert await client.search("서울시청", bounds=bounds) == ()
        assert client.last_warnings == ("SCHEMA_MISMATCH",)
        assert await client.search("서울도서관", bounds=bounds) == ()
        assert client.last_warnings == ("RESPONSE_TOO_LARGE",)


@pytest.mark.asyncio
async def test_streaming_size_failure_is_not_retried_or_reclassified() -> None:
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            stream=_ChunkedBody(),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = KakaoLocalClient(
            http=http,
            rest_key=SecretStr("test-key"),
            max_response_bytes=100,
        )
        result = await client.search(
            "서울시청",
            bounds=BoundingBox(126.7, 37.4, 127.3, 37.75),
        )

    assert result == ()
    assert client.last_warnings == ("RESPONSE_TOO_LARGE",)
    assert requests == 1


@pytest.mark.asyncio
async def test_owned_local_client_aclose_is_idempotent() -> None:
    client = KakaoLocalClient(rest_key=SecretStr("test-key"))
    owned_http = client._http

    await client.aclose()
    await client.aclose()

    assert owned_http.is_closed
