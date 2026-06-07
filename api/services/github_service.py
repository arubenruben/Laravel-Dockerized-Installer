from enum import Enum
from typing import List, Optional

import httpx

GITHUB_RELEASES_API = "https://api.github.com/repos/laravel/laravel/releases"
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_version_enum: Optional[type] = None


async def initialize_versions() -> None:
    """Fetch all Laravel releases from GitHub and cache them as an Enum."""
    global _version_enum
    versions = await _fetch_laravel_versions()
    _version_enum = _build_version_enum(versions)


def get_version_enum() -> Optional[type]:
    """Return the cached version Enum, or None if not yet initialised."""
    return _version_enum


async def download_release_zip(version: str) -> bytes:
    """Download the GitHub source zip for a given Laravel release tag."""
    url = f"https://github.com/laravel/laravel/archive/refs/tags/{version}.zip"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=GITHUB_HEADERS)
        if resp.status_code != 200:
            raise RuntimeError(f"GitHub returned {resp.status_code} for {url}")
        return resp.content


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _fetch_laravel_versions() -> List[str]:
    async with httpx.AsyncClient(timeout=15.0) as client:
        versions: List[str] = []
        page = 1
        while True:
            resp = await client.get(
                GITHUB_RELEASES_API,
                headers=GITHUB_HEADERS,
                params={"per_page": 100, "page": page},
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            versions.extend(r["tag_name"] for r in data if not r.get("draft"))
            if len(data) < 100:
                break
            page += 1
        return versions


def _build_version_enum(versions: List[str]) -> type:
    return Enum("LaravelVersion", {v: v for v in versions})  # type: ignore[return-value]
