import logging
from typing import List, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from services.installer_service import (
    AUTH_FEATURE_KEYS,
    INERTIA_SERVER_TEMPLATES_V2,
    build_inertia_project_zip,
    slugify_app_name,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2", tags=["v2"])

PHP_VERSION_PATTERN = r"^\d+\.\d+$"


# ── Inertia starter-kit route ────────────────────────────────────────────────


@router.get(
    "/new-inertia",
    summary="Download a Docker-ready Laravel + Inertia.js project zip (php-fpm + nginx, with stage/prod stacks)",
    description=(
        "Runs `laravel new` **on the server**, installs all dependencies, and returns a "
        "complete Laravel + Inertia.js project zip ready for Docker.\n\n"
        "Unlike `/v1/new-inertia`, the generated image runs **php-fpm + nginx** "
        "(per Laravel's deployment recommendations) instead of `php artisan serve`, "
        "builds frontend assets and installs Composer dependencies at image build time, "
        "and ships staging/production Compose stacks alongside the dev one.\n\n"
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
    tags=["v2"],
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
        pattern=PHP_VERSION_PATTERN,
        description="PHP version used in the generated Dockerfile (e.g. 8.4, 8.2).",
    ),
    app_port: int = Query(
        default=8080,
        ge=1024,
        le=65535,
        description="Host port mapped to the Laravel app container (port 8000 inside in dev, 80 in stage/prod).",
    ),
    db: Literal["mysql", "postgres", "sqlite"] = Query(
        default="mysql",
        description="Database engine to configure in the Compose stacks and .env.",
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

    - ``Dockerfile`` – PHP ``php_version``-fpm image; builds assets (``npm run build``) and
      installs Composer dependencies at build time, serves via nginx + php-fpm
    - ``docker-compose.yml`` – dev stack: app (``php artisan serve``) + **vite** (Node 20) +
      db, app on ``app_port``, Vite on 5173
    - ``docker-compose.stage.yml`` / ``docker-compose.prod.yml`` – nginx + php-fpm stacks built
      from the same Dockerfile, no source bind mount or Vite sidecar, secrets via env vars
    - ``docker/nginx.conf``, ``docker/dev.entrypoint.sh``, ``docker/prod.entrypoint.sh`` –
      nginx vhost and the dev/stage-prod entrypoint scripts
    - ``.dockerignore`` – excludes ``.git``, ``node_modules``, ``vendor``, ``public/build``,
      ``.env*``, and log files from the build context
    - ``.env`` / ``.env.docker`` – pre-filled environment variables including a generated ``APP_KEY``

    ``vendor/``, ``node_modules/``, and ``.git/`` are excluded from the archive —
    they are installed/created when the dev containers start.
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
        output_buffer = await build_inertia_project_zip(
            template_context, templates=INERTIA_SERVER_TEMPLATES_V2
        )
    except Exception as exc:
        logger.exception("Project generation failed for app_name=%r", app_name)
        raise HTTPException(
            status_code=500,
            detail="Project generation failed. Please try again or report this issue.",
        ) from exc

    app_slug = slugify_app_name(app_name)
    filename = f"laravel-{app_slug}-inertia-{starter_kit}-docker.zip"
    return StreamingResponse(
        output_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
