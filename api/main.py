from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse

from dto.responses.health_response import HealthResponse
from dto.router.v1 import router as v1_router
from services import github_service


# ---------------------------------------------------------------------------
# Lifespan: initialise the version list at startup
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    await github_service.initialize_versions()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Laravel Dockerized Installer",
    description="Download a Docker-ready Laravel project for any official release.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(v1_router)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["utility"], response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return HealthResponse(status="healthy")


# ---------------------------------------------------------------------------
# Patch OpenAPI schema to surface dynamic version enum in Swagger UI
# ---------------------------------------------------------------------------


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    version_enum = github_service.get_version_enum()
    if version_enum is not None:
        versions_list = [e.value for e in version_enum]  # type: ignore[attr-defined]
        try:
            param = schema["paths"]["/v1/release"]["get"]["parameters"][0]
            param["schema"]["enum"] = versions_list
            param["schema"]["example"] = versions_list[0] if versions_list else None
        except (KeyError, IndexError):
            pass
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]
