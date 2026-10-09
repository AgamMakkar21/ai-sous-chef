from fastapi import APIRouter
from fastapi.responses import JSONResponse

from sous_chef.api.problems import problem_response
from sous_chef.services.readiness import ReadinessService


def create_health_router(service: ReadinessService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/ready", response_model=None)
    async def ready() -> JSONResponse:
        if not await service.is_ready():
            return problem_response(
                503,
                "dependency_unavailable",
                "Service unavailable",
                "The service is not ready. Try again later.",
            )
        return JSONResponse({"status": "ready"}, headers={"Cache-Control": "no-store"})

    return router
