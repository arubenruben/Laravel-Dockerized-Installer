from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from services import github_service
from services.installer_service import build_docker_zip

router = APIRouter(prefix="/v1", tags=["v1"])


@router.get(
    "/download",
    summary="Download a Docker-ready Laravel zip",
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
):
    """
    Downloads the official Laravel source zip for the requested **version**,
    then injects Docker scaffold files:

    - `Dockerfile` – PHP 8.3-fpm image
    - `docker-compose.yml` – app + nginx + MySQL 8 + Redis 7
    - `docker/nginx/default.conf` – Nginx virtual host
    - `.env.docker` – pre-filled environment variables for Docker

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

    output_buffer = build_docker_zip(upstream_zip_bytes)

    filename = f"laravel-{version}-docker.zip"
    return StreamingResponse(
        output_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
