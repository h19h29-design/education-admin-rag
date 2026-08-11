from typing import Protocol

from app.routing.models import ProviderResult, RouteQuery, TravelMode


class RouteProvider(Protocol):
    name: str
    supported_modes: frozenset[TravelMode]

    async def get_routes(self, query: RouteQuery) -> ProviderResult:
        raise NotImplementedError
