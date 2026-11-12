from time import monotonic

from opentelemetry import trace
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from sous_chef.api.problems import problem_response
from sous_chef.telemetry import (
    ErrorCategory,
    Event,
    error_category,
    new_trace_id,
    operational_event,
    trace_id,
)
from sous_chef.tracing import Operation, Telemetry, fail, operation


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with self.telemetry.activate(), operation(Operation.HTTP, root=True):
            await self._request(scope, receive, send)

    async def _request(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = new_trace_id()
        span = trace.get_current_span()
        span.set_attribute("asc.correlation_id", request_id)
        method = scope.get("method", "")
        span.set_attribute(
            "http.request.method",
            method
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
            else "OTHER",
        )
        trace_token = trace_id.set(request_id)
        category_token = error_category.set(None)
        started_at = monotonic()
        response_started = False
        status = 500

        async def safe_send(message: Message) -> None:
            nonlocal response_started, status
            if message["type"] == "http.response.start":
                status = message["status"]
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-trace-id"
                ] + [(b"x-trace-id", request_id.encode("ascii"))]
                response_started = True
            await send(message)

        try:
            try:
                await self.app(scope, receive, safe_send)
            except Exception:
                status = 500
                error_category.set(ErrorCategory.INTERNAL_ERROR)
                # A started stream cannot be replaced with a second HTTP response.
                if response_started:
                    raise
                response = problem_response(
                    500,
                    "internal_error",
                    "Internal error",
                    "The request failed. Use the trace ID for support.",
                )
                await response(scope, receive, safe_send)
        finally:
            span.set_attribute("http.response.status_code", status)
            route = getattr(scope.get("route"), "path", None)
            if isinstance(route, str):
                span.set_attribute("http.route", route)
            category = error_category.get()
            if status >= 400 and category is None:
                category = (
                    ErrorCategory.INTERNAL_ERROR if status >= 500 else ErrorCategory.HTTP_ERROR
                )
            if category is not None:
                fail(category)
            operational_event(
                Event.REQUEST_FAILED if status >= 400 else Event.REQUEST_COMPLETED,
                category=category,
                status=status,
                duration_ms=(monotonic() - started_at) * 1000,
            )
            trace_id.reset(trace_token)
            error_category.reset(category_token)
