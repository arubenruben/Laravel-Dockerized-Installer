from typing import List, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from services import github_service
from services.installer_service import AUTH_FEATURE_KEYS, build_docker_zip, build_inertia_project_zip

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get(
    "/release",
    summary="Get a Docker-ready Laravel release zip",
    response_description="A zip archive containing the Laravel source and Docker scaffold files.",
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Laravel zip ready to use with Docker",
        },
        422: {"description": "Unknown or invalid version supplied"},
        502: {"description": "Failed to fetch release zip from GitHub"},
        503: {"description": "Version list not yet available"},
    },
)
async def download(
    version: str = Query(
        ...,
        description="Laravel release version to download",
    ),
    php_version: str = Query(
        default="8.4",
        description="PHP version used in the Dockerfile (e.g. 8.4, 8.4)",
    ),
    app_port: int = Query(
        default=8080,
        ge=1024,
        le=65535,
        description="Host port mapped to Nginx port 80 in docker-compose.yml",
    ),
    db: Literal["mysql", "postgres", "sqlite"] = Query(
        ...,
        description="Database engine to use",
    ),
    app_name: str = Query(
        default="Laravel",
        description="Application name (APP_NAME in .env)",
    ),
):
    """
    Downloads the official Laravel source zip for the requested **version**,
    then renders and injects Docker scaffold files:

    - `Dockerfile` – PHP `php_version`-fpm image (default: 8.4)
    - `docker-compose.yml` – app + nginx + MySQL 8 + Redis 7, exposed on `app_port` (default: 8080)
    - `docker/nginx/default.conf` – Nginx virtual host
    - `.env.docker` – pre-filled environment variables for Docker

    All parameters have defaults — just pick a `version` for a quick start.  
    Run `cp .env.docker .env && docker compose up --build` to get started.
    """
    version_enum = github_service.get_version_enum()
    if version_enum is None:
        raise HTTPException(
            status_code=503,
            detail="Version list not yet available, please retry.",
        )

    valid_versions = {e.value for e in version_enum}  # type: ignore[attr-defined]
    if version not in valid_versions:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown version '{version}'. "
                f"Latest available: {sorted(valid_versions, reverse=True)[:10]} …"
            ),
        )

    try:
        upstream_zip_bytes = await github_service.download_release_zip(version)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    template_context = {
        "laravel_version": version,
        "php_version": php_version,
        "app_port": str(app_port),
        "db": db,
        "app_name": app_name,
    }
    output_buffer = build_docker_zip(upstream_zip_bytes, template_context)

    filename = f"laravel-{version}-docker.zip"
    return StreamingResponse(
        output_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Inertia starter-kit route ────────────────────────────────────────────────


@router.get(
    "/new-inertia",
    summary="Download a Docker-ready Laravel + Inertia.js project zip",
    description=(
        "Runs `laravel new` **on the server**, installs all dependencies, and returns a "
        "complete Laravel + Inertia.js project zip ready for Docker.\n\n"
        "**The only steps required on your machine:**\n"
        "```bash\n"
        "unzip <archive>.zip && cd <app-name>\n"
        "docker compose up --build\n"
        "```\n\n"
        "> **Note:** This request runs `laravel new` server-side and may take "
        "up to a few minutes."
    ),
    response_description="A zip archive containing a fully scaffolded Laravel + Inertia.js project with Docker files.",
    responses={
        200: {
            "content": {"application/zip": {}},
            "description": "Ready-to-run Laravel + Inertia.js Docker project zip",
        },
        500: {"description": "Server-side project generation failed"},
    },
    tags=["v1"],
)
async def new_inertia(
    # ── Inertia-specific parameters ─────────────────────────────────────────
    starter_kit: Literal["react", "vue", "livewire", "livewire-class-components"] = Query(
        default="react",
        description=(
            "Inertia.js starter kit. Maps directly to the `laravel new` CLI flag: "
            "`--react`, `--vue`, `--livewire`, or `--livewire-class-components`."
        ),
    ),
    auth_provider: Literal["laravel", "workos"] = Query(
        default="laravel",
        description=(
            "Authentication provider. `laravel` uses Laravel's built-in auth (Breeze). "
            "`workos` integrates WorkOS AuthKit."
        ),
    ),
    teams: bool = Query(
        default=False,
        description="Add multi-tenancy teams support to the application (`--teams` flag).",
    ),
    testing_framework: Literal["phpunit", "pest"] = Query(
        default="phpunit",
        description="Testing framework to scaffold (`--phpunit` or `--pest`).",
    ),
    install_boost: bool = Query(
        default=False,
        description=(
            "Install `laravel/boost` for AI-assisted coding. "
            "Runs `composer global require laravel/boost --dev` before `laravel new`."
        ),
    ),
    auth_features: List[str] = Query(
        default=[],
        description=(
            "Authentication features to enable. Repeat the parameter for multiple values. "
            "Valid values: `email-verification`, `registration`, `2fa`, `passkeys`, "
            "`password-confirmation`. Defaults to none selected."
        ),
    ),
    # ── Shared Docker parameters ─────────────────────────────────────────────
    php_version: str = Query(
        default="8.4",
        description="PHP version used in the generated Dockerfile (e.g. 8.4, 8.2).",
    ),
    app_port: int = Query(
        default=8080,
        ge=1024,
        le=65535,
        description="Host port mapped to the Laravel app container (port 8000 inside).",
    ),
    db: Literal["mysql", "postgres", "sqlite"] = Query(
        default="mysql",
        description="Database engine to configure in docker-compose.yml and .env.",
    ),
    app_name: str = Query(
        default="my-app",
        description=(
            "Application name passed to `laravel new` and used as the project directory. "
            "Spaces are replaced with hyphens."
        ),
    ),
):
    """
    Runs ``laravel new`` on the server with the requested options, then returns a
    **complete, ready-to-run** zip archive.

    The archive contains the full Laravel project plus:

    - ``Dockerfile`` – PHP ``php_version``-cli image for the Laravel backend
    - ``docker-compose.yml`` – app + **vite** (Node 20) + db + Redis, app on ``app_port``, Vite on 5173
    - ``.env`` / ``.env.docker`` – pre-filled environment variables including a generated ``APP_KEY``

    ``vendor/``, ``node_modules/``, and ``.git/`` are excluded from the archive —
    they are installed/created when the containers start.
    """
    unknown = [f for f in auth_features if f not in AUTH_FEATURE_KEYS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown auth_features: {unknown}. Valid values: {sorted(AUTH_FEATURE_KEYS)}",
        )

    template_context = {
        "php_version": php_version,
        "app_port": str(app_port),
        "db": db,
        "app_name": app_name,
        "starter_kit": starter_kit,
        "auth_provider": auth_provider,
        "teams": teams,
        "testing_framework": testing_framework,
        "install_boost": install_boost,
        "auth_features": auth_features,
    }

    try:
        output_buffer = await build_inertia_project_zip(template_context)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Project generation failed: {exc}",
        ) from exc

    app_slug = app_name.lower().replace(" ", "-")
    filename = f"laravel-{app_slug}-inertia-{starter_kit}-docker.zip"
    return StreamingResponse(
        output_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
