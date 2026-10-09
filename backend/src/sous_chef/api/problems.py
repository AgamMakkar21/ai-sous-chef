import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

logger = logging.getLogger(__name__)


def problem_response(status: int, code: str, title: str, detail: str) -> JSONResponse:
    trace_id = uuid4().hex
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers={"X-Trace-ID": trace_id, "Cache-Control": "no-store"},
        content={
            "type": "urn:ai-sous-chef:problem:" + code.replace("_", "-"),
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "traceId": trace_id,
            "errors": [],
        },
    )


def register_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return problem_response(404, "not_found", "Not found", "The resource was not found.")
        return problem_response(
            exc.status_code, "http_error", HTTPStatus(exc.status_code).phrase, "The request failed."
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        malformed_json = any(error["type"] == "json_invalid" for error in exc.errors())
        return problem_response(
            400 if malformed_json else 422,
            "malformed_json" if malformed_json else "invalid_input",
            "Invalid request",
            "Check the request format and field values.",
        )

    # Catch at the boundary so the ASGI server cannot log raw exception contents.
    @app.middleware("http")
    async def internal_error(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            return await call_next(request)
        except Exception:
            response = problem_response(
                500,
                "internal_error",
                "Internal error",
                "The request failed. Use the trace ID for support.",
            )
            logger.error("request_failed trace_id=%s", response.headers["X-Trace-ID"])
            return response
