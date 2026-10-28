import json
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import UUID4

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.revisions import (
    Revision,
    require_no_query,
    require_revision,
    set_revision_headers,
)
from sous_chef.domain.groceries import (
    CreateGrocery,
    GroceryEnvelope,
    GroceryList,
    UpdateGrocery,
)
from sous_chef.services.groceries import GroceryService
from sous_chef.settings import Settings


class _GroceryRequest(Request):
    async def json(self) -> Any:
        return json.loads(await self.body(), parse_float=Decimal)


class _GroceryRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def preserve_decimal_input(request: Request) -> Response:
            return await handler(_GroceryRequest(request.scope, request.receive))

        return preserve_decimal_input


def create_grocery_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/v1/groceries", tags=["groceries"], route_class=_GroceryRoute)
    service = GroceryService(settings)

    @router.get("")
    async def list_groceries(
        user: CurrentUser, request: Request, response: Response
    ) -> GroceryList:
        require_no_query(request)
        groceries, etag = await service.list(user)
        set_revision_headers(response, etag)
        return groceries

    @router.post("", status_code=201)
    async def add(
        payload: CreateGrocery,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> GroceryEnvelope:
        revision = require_revision(request, if_match)
        grocery, etag = await service.add(user, payload, revision)
        set_revision_headers(response, etag)
        response.headers["Location"] = f"/api/v1/groceries/{grocery.grocery_id}"
        return GroceryEnvelope(data=grocery)

    @router.patch("/{grocery_id}")
    async def update(
        grocery_id: UUID4,
        payload: UpdateGrocery,
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
    ) -> GroceryEnvelope:
        revision = require_revision(request, if_match)
        grocery, etag = await service.update(user, grocery_id, payload, revision)
        set_revision_headers(response, etag)
        return GroceryEnvelope(data=grocery)

    @router.delete("/{grocery_id}", status_code=204)
    async def delete(
        grocery_id: UUID4,
        user: CurrentUser,
        request: Request,
        if_match: Revision,
    ) -> Response:
        revision = require_revision(request, if_match)
        if await request.body():
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("body",)}])
        etag = await service.delete(user, grocery_id, revision)
        response = Response(status_code=204)
        set_revision_headers(response, etag)
        return response

    return router
