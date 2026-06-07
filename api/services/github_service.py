import json
import logging
import os
from enum import Enum
from typing import List, Optional

import httpx

GITHUB_RELEASES_API = "https://api.github.com/repos/laravel/laravel/releases"
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".laravel_versions_cache.json")

_version_enum: Optional[type] = None

logger = logging.getLogger(__name__)


async def initialize_versions() -> None:
    """Fetch all Laravel releases from GitHub and cache them as an Enum."""
    global _version_enum
    versions = await _fetch_laravel_versions()
    _version_enum = _build_version_enum(versions)


def _read_cache() -> Optional[List[str]]:
    """Return cached versions from disk, or None if unavailable."""
    try:
        with open(_CACHE_FILE, "r") as f:
            data = json.load(f)
        logger.info("Loaded Laravel versions from local cache (%s).", _CACHE_FILE)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_cache(versions: List[str]) -> None:
    """Persist versions to disk to avoid redundant GitHub API calls."""
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w") as f:
            json.dump(versions, f)
        logger.info("Saved Laravel versions to local cache (%s).", _CACHE_FILE)
    except OSError as exc:
        logger.warning("Could not write version cache: %s", exc)


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
    cached = _read_cache()
    if cached is not None:
        return cached

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

    _write_cache(versions)
    return versions


def _build_version_enum(versions: List[str]) -> type:
    return Enum("LaravelVersion", {v: v for v in versions})  # type: ignore[return-value]
