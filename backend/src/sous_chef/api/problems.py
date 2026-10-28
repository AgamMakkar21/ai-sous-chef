from http import HTTPStatus
from math import ceil

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from sous_chef.domain.authorization import OwnedResourceNotFound
from sous_chef.domain.conversations import IdempotencyConflict, InvalidCursor
from sous_chef.domain.cosmos import CosmosError, CosmosFailure
from sous_chef.domain.groceries import GROCERY_FIELDS, DuplicateGrocery, GroceryLimitReached
from sous_chef.domain.profiles import PREFERENCE_FIELDS, OnboardingRequired
from sous_chef.settings import ConfigurationError
from sous_chef.telemetry import ErrorCategory, error_category, new_trace_id, trace_id


def problem_response(
    status: int, code: str, title: str, detail: str, *, errors: list[dict[str, str]] | None = None
) -> JSONResponse:
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
            "errors": errors or [],
        },
    )


def register_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(DuplicateGrocery)
    async def duplicate_grocery(_request: Request, exc: DuplicateGrocery) -> JSONResponse:
        return problem_response(
            409,
            "duplicate_grocery",
            "Grocery already exists",
            "A grocery with this name and unit already exists.",
            errors=[{"field": "name", "code": "duplicate", "groceryId": str(exc.grocery_id)}],
        )

    @app.exception_handler(GroceryLimitReached)
    async def grocery_limit(_request: Request, _exc: GroceryLimitReached) -> JSONResponse:
        return problem_response(
            422,
            "grocery_limit_reached",
            "Inventory is full",
            "Remove an existing grocery before adding another.",
            errors=[{"field": "groceries", "code": "too_many_items"}],
        )

    @app.exception_handler(InvalidCursor)
    async def invalid_cursor(_request: Request, _exc: InvalidCursor) -> JSONResponse:
        return problem_response(
            400, "invalid_cursor", "Invalid cursor", "Restart listing with a new first page."
        )

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_request: Request, _exc: IdempotencyConflict) -> JSONResponse:
        return problem_response(
            409,
            "idempotency_conflict",
            "Creation key already used",
            "Use the original request with this key, or a new key for a new conversation.",
        )

    @app.exception_handler(OnboardingRequired)
    async def onboarding_required(_request: Request, _exc: OnboardingRequired) -> JSONResponse:
        return problem_response(
            403,
            "onboarding_required",
            "Onboarding required",
            "Complete all required onboarding fields before continuing.",
        )

    @app.exception_handler(ConfigurationError)
    async def unavailable_configuration(
        _request: Request, _exc: ConfigurationError
    ) -> JSONResponse:
        return problem_response(
            503,
            "configuration_unavailable",
            "Service not configured",
            "The operation is unavailable. Contact the service owner.",
        )

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
        grocery = _request.url.path == "/api/v1/groceries" or _request.url.path.startswith(
            "/api/v1/groceries/"
        )
        if (
            grocery or _request.url.path in {"/api/v1/me/onboarding", "/api/v1/me/preferences"}
        ) and any(
            error["type"] == "missing" and error["loc"] == ("header", "If-Match")
            for error in exc.errors()
        ):
            return problem_response(
                428,
                "precondition_required",
                "A current revision is required",
                "Retrieve the profile or grocery list and send its ETag in If-Match.",
            )
        malformed_json = any(error["type"] == "json_invalid" for error in exc.errors())
        fields: list[dict[str, str]] = []
        if not malformed_json and (
            grocery
            or _request.url.path
            in {
                "/api/v1/me",
                "/api/v1/me/onboarding",
                "/api/v1/me/preferences",
            }
        ):
            codes = {
                "missing": "required",
                "extra_forbidden": "unknown_field",
                "string_too_short": "blank",
                "string_too_long": "too_long",
                "too_long": "too_many_items",
                "greater_than_equal": "out_of_range",
                "less_than_equal": "out_of_range",
                "greater_than": "must_be_positive",
                "decimal_max_places": "too_many_decimal_places",
                "unsupported_safety_constraint": "unsupported_constraint",
                "null_not_allowed": "null_not_allowed",
                "empty_update": "empty_update",
            }
            allowed_fields = GROCERY_FIELDS if grocery else PREFERENCE_FIELDS
            for error in exc.errors():
                location = error["loc"]
                field = (
                    ("groceries" if grocery else "preferences")
                    if location == ("body",)
                    else "request"
                )
                if len(location) >= 2 and location[0] == "body" and location[1] in allowed_fields:
                    field = str(location[1])
                    if len(location) == 3 and type(location[2]) is int:
                        field += "." + str(location[2])
                elif location == ("header", "If-Match"):
                    field = "If-Match"
                elif grocery and location == ("path", "grocery_id"):
                    field = "groceryId"
                entry = {"field": field, "code": codes.get(error["type"], "invalid_value")}
                if entry not in fields:
                    fields.append(entry)
                    if len(fields) == 32:
                        break
        return problem_response(
            400 if malformed_json else 422,
            "malformed_json" if malformed_json else "invalid_input",
            "Invalid request",
            "Check the request format and field values.",
            errors=fields,
        )
