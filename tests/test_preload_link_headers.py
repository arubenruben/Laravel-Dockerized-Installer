"""
``AddLinkHeadersForPreloadedAssets`` copies every asset Vite preloads into a
``Link`` response header; behind a reverse proxy that header outgrows the
proxy's buffers and the request fails with a 502. The installer removes the
middleware from ``bootstrap/app.php`` right next to its ``trustProxies`` patch.

``_run`` is replaced by a recorder, so no Composer, PHP or npm is needed to
generate; ``php -l`` is only used to lint the result when ``php`` is installed.
"""

import io
import shutil
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from main import app
from services import installer_service

# ``bootstrap/app.php`` exactly as the react/vue starter kits ship it.
STARTER_KIT_APP_PHP = """<?php

use App\\Http\\Middleware\\HandleAppearance;
use App\\Http\\Middleware\\HandleInertiaRequests;
use Illuminate\\Foundation\\Application;
use Illuminate\\Foundation\\Configuration\\Exceptions;
use Illuminate\\Foundation\\Configuration\\Middleware;
use Illuminate\\Http\\Middleware\\AddLinkHeadersForPreloadedAssets;
use Illuminate\\Http\\Request;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        commands: __DIR__.'/../routes/console.php',
        health: '/up',
    )
    ->withMiddleware(function (Middleware $middleware): void {
        $middleware->encryptCookies(except: ['appearance', 'sidebar_state']);

        $middleware->web(append: [
            HandleAppearance::class,
            HandleInertiaRequests::class,
            AddLinkHeadersForPreloadedAssets::class,
        ]);
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        $exceptions->shouldRenderJsonWhen(
            fn (Request $request) => $request->is('api/*'),
        );
    })->create();
"""

COMMENT_HEAD = "        // Deliberately not registering AddLinkHeadersForPreloadedAssets: it\n"


def _write_app_php(project_dir, content=STARTER_KIT_APP_PHP):
    app_php = project_dir / "bootstrap" / "app.php"
    app_php.parent.mkdir(parents=True, exist_ok=True)
    app_php.write_text(content)
    return app_php


def _assert_unregistered(source: str) -> None:
    assert "AddLinkHeadersForPreloadedAssets::class" not in source
    assert "use Illuminate\\Http\\Middleware\\AddLinkHeadersForPreloadedAssets;" not in source


def _php_lint(path) -> None:
    if shutil.which("php") is None:
        pytest.skip("php is not installed")
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_removes_registration_and_import(tmp_path):
    app_php = _write_app_php(tmp_path)

    installer_service._remove_preload_link_headers(tmp_path)

    patched = app_php.read_text()
    _assert_unregistered(patched)
    assert "            HandleInertiaRequests::class,\n        ]);" in patched
    _php_lint(app_php)


def test_explains_the_removal_above_the_web_group_with_matching_indentation(tmp_path):
    app_php = _write_app_php(tmp_path)

    installer_service._remove_preload_link_headers(tmp_path)

    patched = app_php.read_text()
    assert patched.count(COMMENT_HEAD) == 1
    before_web_group = patched.split("        $middleware->web(append: [\n")[0]
    comment = before_web_group[before_web_group.index(COMMENT_HEAD) :]
    assert comment.count("\n") > 1
    assert all(line.startswith("        // ") for line in comment.splitlines())


def test_composes_with_trusted_proxies_patch(tmp_path):
    app_php = _write_app_php(tmp_path)

    installer_service._configure_trusted_proxies(tmp_path)
    installer_service._remove_preload_link_headers(tmp_path)

    patched = app_php.read_text()
    _assert_unregistered(patched)
    assert "$middleware->trustProxies(" in patched
    assert patched.index("$middleware->trustProxies(") < patched.index(COMMENT_HEAD)
    _php_lint(app_php)


def test_running_twice_leaves_the_file_unchanged(tmp_path):
    app_php = _write_app_php(tmp_path)

    installer_service._remove_preload_link_headers(tmp_path)
    once = app_php.read_text()
    installer_service._remove_preload_link_headers(tmp_path)

    assert app_php.read_text() == once


def test_does_nothing_when_the_middleware_is_not_registered(tmp_path):
    without = STARTER_KIT_APP_PHP.replace(
        "            AddLinkHeadersForPreloadedAssets::class,\n", ""
    ).replace("use Illuminate\\Http\\Middleware\\AddLinkHeadersForPreloadedAssets;\n", "")
    app_php = _write_app_php(tmp_path, without)

    installer_service._remove_preload_link_headers(tmp_path)

    assert app_php.read_text() == without


def test_does_nothing_without_bootstrap_app_php(tmp_path):
    installer_service._remove_preload_link_headers(tmp_path)

    assert not (tmp_path / "bootstrap").exists()


@pytest.fixture
def client(monkeypatch):
    def fake_run(cmd, *, cwd, env, timeout, check=True):
        if cmd[:2] == ["composer", "create-project"]:
            project = installer_service.Path(cwd) / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
            _write_app_php(project)
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            (installer_service.Path(cwd) / ".env").write_text("APP_KEY=base64:test\n")

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return TestClient(app)


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_generated_project_from_both_endpoints_lacks_the_middleware(client, version):
    response = client.get(
        f"/{version}/new-inertia", params={"app_name": "demo", "db": "sqlite"}
    )
    assert response.status_code == 200, response.text

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    patched = archive.read("demo/bootstrap/app.php").decode()
    _assert_unregistered(patched)
    assert "$middleware->trustProxies(" in patched
    assert COMMENT_HEAD in patched
