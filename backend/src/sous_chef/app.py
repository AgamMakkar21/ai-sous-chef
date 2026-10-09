from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from sous_chef.adapters.readiness import LocalReadinessProbe, UnconfiguredReadinessProbe
from sous_chef.api.health import create_health_router
from sous_chef.api.problems import problem_response, register_problem_handlers
from sous_chef.domain.readiness import ReadinessProbe
from sous_chef.services.readiness import ReadinessService
from sous_chef.settings import ConfigurationError, Settings, load_settings


def create_app(
    settings: Settings | None = None, *, readiness_probe: ReadinessProbe | None = None
) -> FastAPI:
    config = settings if settings is not None else load_settings()
    probe = readiness_probe
    if probe is None:
        probe = (
            LocalReadinessProbe()
            if config.adapter_mode == "local"
            else UnconfiguredReadinessProbe()
        )
    if isinstance(probe, LocalReadinessProbe) and (
        config.environment != "local" or config.adapter_mode != "local"
    ):
        raise ConfigurationError("Local readiness doubles cannot run outside explicit local mode.")

    app = FastAPI(
        title="AI Sous Chef",
        docs_url="/docs" if config.environment == "local" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.environment == "local" else None,
    )
    register_problem_handlers(app)
    app.include_router(
        create_health_router(ReadinessService(probe, config.readiness_timeout_seconds))
    )

    @app.api_route(
        "/api/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def unimplemented_api(path: str) -> JSONResponse:
        return problem_response(404, "not_found", "Not found", "The resource was not found.")

    if (config.static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=config.static_dir, html=True), name="frontend")
    else:

        @app.get("/", include_in_schema=False)
        async def frontend_unavailable() -> JSONResponse:
            return problem_response(
                503,
                "frontend_unavailable",
                "Frontend unavailable",
                "The frontend build is not installed.",
            )

    return app
