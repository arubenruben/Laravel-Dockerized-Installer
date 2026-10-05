"""
Exercises ``GET /v1/release`` end to end (only GitHub is stubbed), so the
template context is exactly the one the handler builds.

The Jinja environment uses ``StrictUndefined``: if a template references a
variable the release flow doesn't supply, the request raises instead of
silently rendering an empty string.
"""

import io
import zipfile
from enum import Enum

import pytest
from fastapi.testclient import TestClient

from main import app
from services import github_service
from services.installer_service import TEMPLATES

VERSION = "v12.0.0"
ROOT = "laravel-framework-12.0.0/"


def _upstream_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(ROOT + "composer.json", "{}")
    return buffer.getvalue()


@pytest.fixture
def client(monkeypatch):
    versions = Enum("LaravelVersion", {"latest": VERSION})

    async def fake_download(version: str) -> bytes:
        return _upstream_zip()

    monkeypatch.setattr(github_service, "get_version_enum", lambda: versions)
    monkeypatch.setattr(github_service, "download_release_zip", fake_download)
    return TestClient(app)


def _get_release(client: TestClient, **params) -> zipfile.ZipFile:
    response = client.get("/v1/release", params={"version": VERSION, "db": "mysql", **params})
    assert response.status_code == 200
    return zipfile.ZipFile(io.BytesIO(response.content))


@pytest.mark.parametrize("db", ["mysql", "postgres", "sqlite"])
@pytest.mark.parametrize("dest_path", [dest for _, dest in TEMPLATES])
def test_release_renders_every_template(client, db, dest_path):
    archive = _get_release(client, db=db)

    assert ROOT + dest_path in archive.namelist()


def test_release_writes_env_file(client):
    archive = _get_release(client)

    assert ROOT + ".env" in archive.namelist()


@pytest.mark.parametrize(
    ("app_name", "slug"),
    [
        (None, "laravel"),
        ("My App!", "my-app"),
    ],
)
def test_release_compose_containers_use_app_slug(client, app_name, slug):
    params = {} if app_name is None else {"app_name": app_name}
    archive = _get_release(client, **params)

    compose = archive.read(ROOT + "docker-compose.yml").decode()

    assert f"container_name: {slug}_app" in compose
    assert f"container_name: {slug}_db" in compose
