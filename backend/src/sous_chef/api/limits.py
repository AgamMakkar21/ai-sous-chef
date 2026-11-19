from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sous_chef.api.problems import problem_response
from sous_chef.domain.receipts import MAX_RECEIPT_BYTES, RECEIPT_PATH

MAX_BODY_BYTES = 128 * 1024


class ProductBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, receipts_enabled: bool = False) -> None:
        self.app = app
        self.receipts_enabled = receipts_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not scope["path"].startswith("/api/v1/")
            or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        limit = (
            MAX_RECEIPT_BYTES
            if self.receipts_enabled and scope["path"] == RECEIPT_PATH and scope["method"] == "POST"
            else MAX_BODY_BYTES
        )
        lengths = headers.getlist("content-length")
        if lengths and (
            len(lengths) != 1
            or len(lengths[0]) > 20
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
        ):
            await problem_response(
                400, "invalid_input", "Invalid request", "Invalid request length."
            )(scope, receive, send)
            return
        oversized = bool(lengths and int(lengths[0]) > limit)
        body = bytearray()
        while not oversized:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            oversized = len(body) + len(chunk) > limit
            if not oversized:
                body.extend(chunk)
            if not message.get("more_body", False):
                break
        if oversized:
            await problem_response(
                413, "body_too_large", "Request too large", "Reduce the request body size."
            )(scope, receive, send)
            return

        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, send)
