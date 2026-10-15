import json
import re
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from sous_chef.api.problems import problem_response
from sous_chef.services.readiness import ReadinessService
from sous_chef.settings import ConfigurationError


def create_health_router(service: ReadinessService) -> APIRouter:
    router = APIRouter()
    headers = {"Cache-Control": "no-store"}
    release = Path(__file__).parents[1] / "release.json"
    if release.is_file():
        sha = json.loads(release.read_text(encoding="utf-8"))["commit"]
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ConfigurationError("The deployment release metadata contains an invalid commit.")
        headers["X-ASC-Commit"] = sha

    @router.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok"}, headers=headers)

    @router.get("/ready", response_model=None)
    async def ready() -> JSONResponse:
        if not await service.is_ready():
            return problem_response(
                503,
                "dependency_unavailable",
                "Service unavailable",
                "The service is not ready. Try again later.",
            )
        return JSONResponse({"status": "ready"}, headers=headers)

    return router
