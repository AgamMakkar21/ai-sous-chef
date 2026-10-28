from typing import Annotated

from fastapi import Header, Request, Response
from fastapi.exceptions import RequestValidationError

Revision = Annotated[
    str,
    Header(alias="If-Match", min_length=1, max_length=1024, pattern=r"^[^\x00-\x20\x7f]+$"),
]


def require_no_query(request: Request) -> None:
    if request.query_params:
        raise RequestValidationError([{"type": "extra_forbidden", "loc": ("query",)}])


def require_revision(request: Request, etag: str) -> str:
    require_no_query(request)
    if len(request.headers.getlist("if-match")) != 1 or etag == "*" or etag.startswith("W/"):
        raise RequestValidationError([{"type": "value_error", "loc": ("header", "If-Match")}])
    return etag


def set_revision_headers(response: Response, etag: str) -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"
