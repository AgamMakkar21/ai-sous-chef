from http import HTTPStatus
from math import ceil

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sous_chef.domain.authorization import OwnedResourceNotFound
from sous_chef.domain.cosmos import CosmosError, CosmosFailure
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
    @app.exception_handler(CosmosError)
    async def cosmos_failure(_request: Request, exc: CosmosError) -> JSONResponse:
        status, title, detail = {
            CosmosFailure.CONFLICT: (
                412,
                "This item changed",
                "Reload the latest version before trying again.",
            ),
            CosmosFailure.EXISTS: (409, "Already exists", "This record already exists."),
            CosmosFailure.TOO_LARGE: (
                413,
                "Record too large",
                "Reduce the record size before trying again.",
            ),
            CosmosFailure.THROTTLED: (429, "Database busy", "Wait before trying again."),
            CosmosFailure.TIMEOUT: (
                504,
                "Database deadline exceeded",
                "The database operation exceeded its deadline.",
            ),
            CosmosFailure.INVALID_RESULT: (
                502,
                "Invalid database response",
                "The database returned an invalid result.",
            ),
            CosmosFailure.REJECTED: (
                502,
                "Database request rejected",
                "The database rejected the operation.",
            ),
        }.get(exc.code, (503, "Database unavailable", "The database operation is unavailable."))
        if exc.outcome_unknown:
            detail = "The write outcome is unknown. Reconcile its state before trying again."
        response = problem_response(status, exc.code.value, title, detail)
        if exc.retry_after_seconds is not None and not exc.outcome_unknown:
            response.headers["Retry-After"] = str(ceil(exc.retry_after_seconds))
        return response

    @app.exception_handler(OwnedResourceNotFound)
    async def owned_resource_not_found(
        _request: Request, _exc: OwnedResourceNotFound
    ) -> JSONResponse:
        return problem_response(404, "not_found", "Not found", "The resource was not found.")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        if exc.status_code == 401:
            response = problem_response(
                401, "unauthenticated", "Authentication required", "Sign in again to continue."
            )
            response.headers["WWW-Authenticate"] = "Bearer"
            return response
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
