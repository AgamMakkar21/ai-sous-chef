from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from sous_chef.api.authentication import CurrentUser
from sous_chef.api.revisions import Revision, require_revision, set_revision_headers
from sous_chef.domain.receipts import RECEIPT_PATH, ReceiptReader, ReceiptResult
from sous_chef.services.receipts import ReceiptService
from sous_chef.settings import Settings


def create_receipt_router(settings: Settings, reader: ReceiptReader | None = None) -> APIRouter:
    router = APIRouter(tags=["groceries"])
    service = ReceiptService(settings, reader)

    @router.post(RECEIPT_PATH)
    async def import_receipt(
        user: CurrentUser,
        request: Request,
        response: Response,
        if_match: Revision,
        idempotency_key: Annotated[
            str, Header(min_length=1, max_length=128, pattern=r"^[\x21-\x7e]+$")
        ],
    ) -> ReceiptResult:
        if not settings.receipts_enabled:
            raise HTTPException(404)
        revision = require_revision(request, if_match)
        if len(request.headers.getlist("idempotency-key")) != 1:
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("header",)}])
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1 or content_types[0].split(";", 1)[0].strip() not in {
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/heic",
            "image/heif",
            "application/pdf",
            "application/octet-stream",
        }:
            raise RequestValidationError([{"type": "extra_forbidden", "loc": ("body",)}])
        result, etag = await service.import_receipt(
            user, await request.body(), idempotency_key, revision
        )
        set_revision_headers(response, etag)
        return result

    return router
