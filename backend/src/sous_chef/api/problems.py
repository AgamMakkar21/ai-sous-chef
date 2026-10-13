from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sous_chef.telemetry import ErrorCategory, error_category, new_trace_id, trace_id


def problem_response(status: int, code: str, title: str, detail: str) -> JSONResponse:
    request_id = trace_id.get() or new_trace_id()
    if trace_id.get() is not None:
        error_category.set(
            ErrorCategory(code) if code in ErrorCategory else ErrorCategory.HTTP_ERROR
        )
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers={"X-Trace-ID": request_id, "Cache-Control": "no-store"},
        content={
            "type": "urn:ai-sous-chef:problem:" + code.replace("_", "-"),
            "title": title,
            "status": status,
            "detail": detail,
            "code": code,
            "traceId": request_id,
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
