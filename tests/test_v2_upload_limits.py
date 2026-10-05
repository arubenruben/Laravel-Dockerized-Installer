"""
Exercises ``GET /v2/new-inertia`` end to end with ``_run`` replaced by a
recorder (no Composer, PHP or npm needed), so the template context is exactly
the one the handler builds.

``max_upload_mb`` feeds three places that have to agree: PHP's
``upload_max_filesize``, PHP's ``post_max_size`` and nginx's
``client_max_body_size``. The last two are ``max_upload_mb + 2``.
"""

import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from main import app
from services import installer_service

ROOT = "demo/"


@pytest.fixture
def client(monkeypatch):
    def fake_run(cmd, *, cwd, env, timeout, check=True):
        if cmd[:2] == ["composer", "create-project"]:
            project = installer_service.Path(cwd) / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            (installer_service.Path(cwd) / ".env").write_text("APP_KEY=base64:test\n")

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return TestClient(app)


def _get_project(client: TestClient, **params) -> zipfile.ZipFile:
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "db": "sqlite", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _ini_values(archive: zipfile.ZipFile) -> dict[str, str]:
    ini = archive.read(ROOT + "docker/php/app.ini").decode()
    return dict(re.findall(r"^(\w+) = (\S+)$", ini, re.MULTILINE))


def _nginx_directives(archive: zipfile.ZipFile) -> str:
    return archive.read(ROOT + "docker/nginx.conf").decode()


def test_defaults_allow_10m_uploads_in_a_12m_request(client):
    archive = _get_project(client)

    assert _ini_values(archive) == {
        "upload_max_filesize": "10M",
        "post_max_size": "12M",
        "memory_limit": "256M",
    }
    assert "client_max_body_size 12M;" in _nginx_directives(archive)


def test_max_upload_mb_drives_php_and_nginx_limits(client):
    archive = _get_project(client, max_upload_mb=50)

    ini = _ini_values(archive)
    assert ini["upload_max_filesize"] == "50M"
    assert ini["post_max_size"] == "52M"
    assert "client_max_body_size 52M;" in _nginx_directives(archive)


def test_nginx_post_limit_matches_php_post_max_size(client):
    archive = _get_project(client, max_upload_mb=7)

    nginx_limit = re.search(r"client_max_body_size (\d+M);", _nginx_directives(archive))
    assert nginx_limit
    assert nginx_limit.group(1) == _ini_values(archive)["post_max_size"]


def test_nginx_raises_fastcgi_buffers_for_stacked_set_cookie_headers(client):
    nginx = _nginx_directives(_get_project(client))

    assert "fastcgi_buffer_size 16k;" in nginx
    assert "fastcgi_buffers 4 16k;" in nginx


def test_dockerfile_installs_the_ini_after_the_base_image_ones(client):
    dockerfile = _get_project(client).read(ROOT + "Dockerfile").decode()

    assert "COPY docker/php/app.ini /usr/local/etc/php/conf.d/zz-app.ini" in dockerfile


def test_readme_lists_the_ini_file(client):
    readme = _get_project(client).read(ROOT + "README.md").decode()

    assert "`docker/php/app.ini`" in readme


@pytest.mark.parametrize("max_upload_mb", [0, -1, 1025, "abc"])
def test_max_upload_mb_out_of_range_is_rejected(client, max_upload_mb):
    response = client.get("/v2/new-inertia", params={"max_upload_mb": max_upload_mb})

    assert response.status_code == 422


@pytest.mark.parametrize(("max_upload_mb", "post_max"), [(1, "3M"), (1024, "1026M")])
def test_max_upload_mb_bounds_are_accepted(client, max_upload_mb, post_max):
    archive = _get_project(client, max_upload_mb=max_upload_mb)

    assert _ini_values(archive)["post_max_size"] == post_max
