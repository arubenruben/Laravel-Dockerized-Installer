"""
``telescope=true`` on ``GET /v2/new-inertia`` installs ``laravel/telescope`` as a
``require-dev`` package. ``telescope:install`` lists
``App\\Providers\\TelescopeServiceProvider`` in ``bootstrap/providers.php``, and that
class extends ``Laravel\\Telescope\\TelescopeApplicationServiceProvider``, which does
not exist in the ``composer install --no-dev`` stage/prod image, so every request
would fatal. ``_configure_telescope_dev_only`` therefore moves the registration into
``AppServiceProvider::register()`` behind a ``class_exists`` check.

``_run`` is replaced by a recorder that also imitates what ``telescope:install``
writes, so no Composer, PHP or npm is needed to generate; ``php -l`` is only used to
lint the result when ``php`` is installed.
"""

import io
import shutil
import subprocess
import zipfile

import pytest
from fastapi.testclient import TestClient

from main import app
from services import installer_service

ROOT = "demo/"

# ``app/Providers/AppServiceProvider.php`` as the starter kits ship it.
STARTER_KIT_APP_SERVICE_PROVIDER = """<?php

namespace App\\Providers;

use Illuminate\\Support\\ServiceProvider;

class AppServiceProvider extends ServiceProvider
{
    /**
     * Register any application services.
     */
    public function register(): void
    {
        //
    }

    /**
     * Bootstrap any application services.
     */
    public function boot(): void
    {
        //
    }
}
"""

# ``bootstrap/providers.php`` as the starter kits ship it.
STARTER_KIT_PROVIDERS_PHP = """<?php

use App\\Providers\\AppServiceProvider;
use App\\Providers\\FortifyServiceProvider;

return [
    AppServiceProvider::class,
    FortifyServiceProvider::class,
];
"""

# What ``telescope:install`` leaves behind. ``ServiceProvider::addProviderToBootstrapFile``
# rewrites the whole file: every provider fully qualified and sorted, no ``use`` lines.
# (The imported style above only comes back later, when chisel's ``composer lint`` runs Pint.)
PROVIDERS_PHP_AFTER_INSTALL = """<?php

return [
    App\\Providers\\AppServiceProvider::class,
    App\\Providers\\FortifyServiceProvider::class,
    App\\Providers\\TelescopeServiceProvider::class,
];
"""

PROVIDERS_PHP_WITHOUT_TELESCOPE = PROVIDERS_PHP_AFTER_INSTALL.replace(
    "    App\\Providers\\TelescopeServiceProvider::class,\n", ""
)

# The imported form, for a project whose providers file was already formatted.
PROVIDERS_PHP_IMPORTED = STARTER_KIT_PROVIDERS_PHP.replace(
    "use App\\Providers\\FortifyServiceProvider;\n",
    "use App\\Providers\\FortifyServiceProvider;\nuse App\\Providers\\TelescopeServiceProvider;\n",
).replace("];\n", "    TelescopeServiceProvider::class,\n];\n")

EXPECTED_REGISTER_METHOD = """    public function register(): void
    {
        // Telescope is a require-dev package (never installed in the
        // `composer install --no-dev` production/CI image), so it's only
        // safe to register when its base class actually exists.
        if (class_exists(TelescopeApplicationServiceProvider::class)) {
            $this->app->register(TelescopeServiceProvider::class);
        }
    }
"""
REGISTER_CONDITION = "        if (class_exists(TelescopeApplicationServiceProvider::class)) {\n"
REGISTRATION = "            $this->app->register(TelescopeServiceProvider::class);\n"
BASE_CLASS_IMPORT = "use Laravel\\Telescope\\TelescopeApplicationServiceProvider;\n"


def _write(project_dir, providers=PROVIDERS_PHP_AFTER_INSTALL, service_provider=STARTER_KIT_APP_SERVICE_PROVIDER):
    (project_dir / "bootstrap").mkdir(parents=True, exist_ok=True)
    (project_dir / "app" / "Providers").mkdir(parents=True, exist_ok=True)
    providers_php = project_dir / "bootstrap" / "providers.php"
    provider_php = project_dir / "app" / "Providers" / "AppServiceProvider.php"
    providers_php.write_text(providers)
    provider_php.write_text(service_provider)
    return providers_php, provider_php


def _php_lint(path) -> None:
    if shutil.which("php") is None:
        pytest.skip("php is not installed")
    result = subprocess.run(["php", "-l", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


# ── _configure_telescope_dev_only: bootstrap/providers.php ───────────────────


def test_removes_the_fully_qualified_provider_entry(tmp_path):
    providers_php, _ = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert providers_php.read_text() == PROVIDERS_PHP_WITHOUT_TELESCOPE
    _php_lint(providers_php)


def test_removes_an_imported_provider_entry_and_its_use_line(tmp_path):
    providers_php, _ = _write(tmp_path, providers=PROVIDERS_PHP_IMPORTED)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert providers_php.read_text() == STARTER_KIT_PROVIDERS_PHP
    _php_lint(providers_php)


def test_a_leading_backslash_entry_is_removed_too(tmp_path):
    providers_php, _ = _write(
        tmp_path,
        providers=PROVIDERS_PHP_AFTER_INSTALL.replace(
            "    App\\Providers\\Telescope", "    \\App\\Providers\\Telescope"
        ),
    )

    installer_service._configure_telescope_dev_only(tmp_path)

    assert "Telescope" not in providers_php.read_text()


def test_leaves_the_other_providers_alone(tmp_path):
    providers_php, _ = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    patched = providers_php.read_text()
    assert "    App\\Providers\\AppServiceProvider::class,\n    App\\Providers\\FortifyServiceProvider::class,\n" in patched


def test_works_when_providers_php_never_listed_telescope(tmp_path):
    providers_php, _ = _write(tmp_path, providers=STARTER_KIT_PROVIDERS_PHP)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert providers_php.read_text() == STARTER_KIT_PROVIDERS_PHP


def test_raises_when_the_provider_is_still_referenced_after_removal(tmp_path):
    # Not one of the shapes the entry regex knows, e.g. several providers on one line.
    odd = (
        "<?php\n\nreturn [\n"
        "    App\\Providers\\AppServiceProvider::class, App\\Providers\\TelescopeServiceProvider::class,\n"
        "];\n"
    )
    providers_php, provider_php = _write(tmp_path, providers=odd)

    with pytest.raises(RuntimeError, match="TelescopeServiceProvider"):
        installer_service._configure_telescope_dev_only(tmp_path)

    assert provider_php.read_text() == STARTER_KIT_APP_SERVICE_PROVIDER


# ── _configure_telescope_dev_only: AppServiceProvider::register() ────────────


def test_registers_telescope_behind_a_class_exists_check(tmp_path):
    _, provider_php = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    patched = provider_php.read_text()
    assert patched.count(REGISTER_CONDITION) == 1
    assert patched.count(REGISTRATION) == 1
    assert patched.count(BASE_CLASS_IMPORT) == 1
    _php_lint(provider_php)


def test_the_block_replaces_the_register_placeholder(tmp_path):
    _, provider_php = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert EXPECTED_REGISTER_METHOD in provider_php.read_text()


def test_boot_keeps_its_placeholder(tmp_path):
    _, provider_php = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert "public function boot(): void\n    {\n        //\n    }" in provider_php.read_text()


def test_puts_the_block_before_existing_register_statements(tmp_path):
    existing = STARTER_KIT_APP_SERVICE_PROVIDER.replace(
        "        //\n    }\n\n    /**\n     * Bootstrap",
        "        $this->app->singleton('foo', fn () => 1);\n    }\n\n    /**\n     * Bootstrap",
        1,
    )
    _, provider_php = _write(tmp_path, service_provider=existing)

    installer_service._configure_telescope_dev_only(tmp_path)

    patched = provider_php.read_text()
    assert patched.index(REGISTER_CONDITION) < patched.index("$this->app->singleton('foo'")
    _php_lint(provider_php)


def test_adds_the_base_class_import_after_the_existing_ones(tmp_path):
    _, provider_php = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)

    assert (
        "use Illuminate\\Support\\ServiceProvider;\n" + BASE_CLASS_IMPORT + "\nclass AppServiceProvider"
        in provider_php.read_text()
    )


def test_composes_with_the_force_https_scheme_patch(tmp_path):
    _, provider_php = _write(tmp_path)

    installer_service._configure_force_https_scheme(tmp_path)
    installer_service._configure_telescope_dev_only(tmp_path)

    patched = provider_php.read_text()
    assert "URL::forceScheme('https');" in patched
    assert REGISTER_CONDITION in patched
    _php_lint(provider_php)


def test_running_twice_leaves_both_files_unchanged(tmp_path):
    providers_php, provider_php = _write(tmp_path)

    installer_service._configure_telescope_dev_only(tmp_path)
    once = (providers_php.read_text(), provider_php.read_text())
    installer_service._configure_telescope_dev_only(tmp_path)

    assert (providers_php.read_text(), provider_php.read_text()) == once


def test_raises_when_register_cannot_be_found(tmp_path):
    no_register = STARTER_KIT_APP_SERVICE_PROVIDER.replace(
        "    public function register(): void\n    {\n        //\n    }\n\n", ""
    )
    _, provider_php = _write(tmp_path, service_provider=no_register)

    with pytest.raises(RuntimeError, match=r"register\(\)"):
        installer_service._configure_telescope_dev_only(tmp_path)

    assert provider_php.read_text() == no_register


def test_raises_when_there_is_no_app_service_provider(tmp_path):
    providers_php, provider_php = _write(tmp_path)
    provider_php.unlink()

    with pytest.raises(RuntimeError, match="AppServiceProvider.php"):
        installer_service._configure_telescope_dev_only(tmp_path)


# ── Generation flow ──────────────────────────────────────────────────────────


class Call:
    def __init__(self, cmd):
        self.cmd = cmd

    def __repr__(self):
        return " ".join(self.cmd)


@pytest.fixture
def calls(monkeypatch):
    recorded: list[Call] = []

    def fake_run(cmd, *, cwd, env, timeout, check=True):
        recorded.append(Call(cmd))
        project = installer_service.Path(cwd)
        if cmd[:2] == ["composer", "create-project"]:
            project = project / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
            _write(project, providers=STARTER_KIT_PROVIDERS_PHP)
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            (project / ".env").write_text("APP_KEY=base64:test\n")
        elif cmd[:3] == ["php", "artisan", "telescope:install"]:
            (project / "bootstrap" / "providers.php").write_text(PROVIDERS_PHP_AFTER_INSTALL)

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return recorded


@pytest.fixture
def client(calls):
    return TestClient(app)


def _get_project(client: TestClient, version: str = "v2", **params) -> zipfile.ZipFile:
    response = client.get(f"/{version}/new-inertia", params={"app_name": "demo", "db": "sqlite", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _index(calls, *prefix):
    return next(i for i, c in enumerate(calls) if c.cmd[: len(prefix)] == list(prefix))


def _telescope_calls(calls):
    return [c for c in calls if "telescope" in " ".join(c.cmd)]


def test_telescope_is_off_by_default(client, calls):
    archive = _get_project(client)

    assert not _telescope_calls(calls)
    assert "Telescope" not in archive.read(ROOT + "app/Providers/AppServiceProvider.php").decode()
    assert "Telescope" not in archive.read(ROOT + "README.md").decode()


@pytest.mark.parametrize("value", ["maybe", "2", ""])
def test_invalid_telescope_value_is_rejected(client, value):
    response = client.get("/v2/new-inertia", params={"telescope": value})

    assert response.status_code == 422


def test_requires_telescope_as_a_dev_dependency_without_scripts(client, calls):
    _get_project(client, telescope="true")

    require = calls[_index(calls, "composer", "require", "laravel/telescope")]
    assert "--dev" in require.cmd
    assert "--no-scripts" in require.cmd
    assert "--no-interaction" in require.cmd


def test_discovers_packages_between_the_require_and_the_install(client, calls):
    _get_project(client, telescope="true")

    require = _index(calls, "composer", "require", "laravel/telescope")
    install = _index(calls, "php", "artisan", "telescope:install", "--no-interaction")
    discovers = [
        i for i, c in enumerate(calls) if c.cmd[:3] == ["php", "artisan", "package:discover"]
    ]
    assert any(require < i < install for i in discovers)


def test_installs_telescope_before_chisel_runs(client, calls):
    # Same constraint as every Composer change before ``install:features``: chisel
    # deletes itself afterwards, so Telescope has to be in place first.
    _get_project(client, telescope="true")

    install = _index(calls, "php", "artisan", "telescope:install")
    install_features = _index(calls, "php", "artisan", "install:features")
    assert install < install_features


def test_failed_telescope_install_fails_the_generation(client, monkeypatch, calls):
    real_run = installer_service._run

    def failing_run(cmd, *, cwd, env, timeout, check=True):
        real_run(cmd, cwd=cwd, env=env, timeout=timeout, check=check)
        if cmd[:3] == ["php", "artisan", "telescope:install"]:
            raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(installer_service, "_run", failing_run)

    response = client.get("/v2/new-inertia", params={"app_name": "demo", "telescope": "true"})

    assert response.status_code == 500


def test_generated_project_registers_telescope_only_when_installed(client):
    archive = _get_project(client, telescope="true")

    providers = archive.read(ROOT + "bootstrap/providers.php").decode()
    assert "Telescope" not in providers
    service_provider = archive.read(ROOT + "app/Providers/AppServiceProvider.php").decode()
    assert REGISTER_CONDITION in service_provider
    assert BASE_CLASS_IMPORT in service_provider


def test_generated_php_files_lint(client, tmp_path):
    archive = _get_project(client, telescope="true")
    archive.extractall(tmp_path)

    _php_lint(tmp_path / "demo" / "app" / "Providers" / "AppServiceProvider.php")
    _php_lint(tmp_path / "demo" / "bootstrap" / "providers.php")


def test_unpatchable_project_fails_with_a_500_instead_of_a_broken_zip(client, calls, monkeypatch):
    def broken_run(cmd, *, cwd, env, timeout, check=True):
        calls.append(Call(cmd))
        project = installer_service.Path(cwd)
        if cmd[:2] == ["composer", "create-project"]:
            project = project / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
            _write(
                project,
                providers=PROVIDERS_PHP_AFTER_INSTALL,
                service_provider=STARTER_KIT_APP_SERVICE_PROVIDER.replace("register()", "registered()"),
            )

    monkeypatch.setattr(installer_service, "_run", broken_run)

    response = client.get("/v2/new-inertia", params={"app_name": "demo", "telescope": "true"})

    assert response.status_code == 500


def test_v1_never_installs_telescope(client, calls):
    response = client.get("/v1/new-inertia", params={"app_name": "demo", "db": "sqlite"})

    assert response.status_code == 200, response.text
    assert not _telescope_calls(calls)


# ── README ───────────────────────────────────────────────────────────────────


def test_readme_lists_telescope_and_links_it_when_enabled(client):
    readme = _get_project(client, telescope="true", app_port="8123").read(ROOT + "README.md").decode()

    assert "| Laravel Telescope | yes (dev only) |" in readme
    assert "Telescope: `http://localhost:8123/telescope`" in readme
    # A hard line break after the Vite line, or Markdown folds both into one line.
    assert "Vite dev server: `http://localhost:5173`  \nTelescope:" in readme


def test_readme_does_not_mention_telescope_when_disabled(client):
    readme = _get_project(client, telescope="false").read(ROOT + "README.md").decode()

    assert "elescope" not in readme
