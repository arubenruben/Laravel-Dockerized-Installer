"""
Exercises ``GET /v2/new-inertia`` end to end with ``_run`` replaced by a
recorder (no Composer, PHP or npm needed), so the template context is exactly
the one the handler builds.

Staging/production keep ``storage/app`` (and, with ``db=sqlite``, the database
file) on named volumes shared by ``app`` and ``worker``, so uploads and data
survive a recreate and the worker sees what the web container wrote. The prod
entrypoint prepares ``storage/`` as root on every boot (directories, ``www-data``
ownership, the ``public/storage`` link, the SQLite file) and then runs everything
else as ``www-data``. It is rendered and executed here with fake ``su-exec``,
``php``, ``find`` and ``chown`` that print their command line.
"""

import io
import re
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from services import installer_service

ROOT = "demo/"
DBS = ["mysql", "postgres", "sqlite"]
QUEUES = ["database", "redis"]  # the ones with a worker
STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]

STORAGE_MOUNT = "/var/www/html/storage/app"
SQLITE_MOUNT = "/var/www/html/database/data"
SQLITE_FILE = f"{SQLITE_MOUNT}/database.sqlite"
STORAGE_DIRS = [
    "storage/app/private",
    "storage/app/public",
    "storage/logs",
    "storage/framework/cache/data",
    "storage/framework/sessions",
    "storage/framework/views",
    "bootstrap/cache",
]
CHOWN_MISMATCHED = "find storage bootstrap/cache ! -user www-data -exec chown www-data:www-data {} +"


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
        if cmd[:2] == ["composer", "create-project"]:
            project = installer_service.Path(cwd) / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text("APP_KEY=\n")
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            (installer_service.Path(cwd) / ".env").write_text("APP_KEY=base64:test\n")

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return recorded


@pytest.fixture
def client(calls):
    return TestClient(app)


def _get_project(client: TestClient, **params) -> zipfile.ZipFile:
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "db": "sqlite", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _compose(archive: zipfile.ZipFile, name: str) -> dict:
    return yaml.safe_load(archive.read(ROOT + name))


# ── Dockerfile ───────────────────────────────────────────────────────────────


def test_dockerfile_installs_su_exec_so_the_entrypoint_can_drop_to_www_data(client):
    dockerfile = _get_project(client).read(ROOT + "Dockerfile").decode()

    assert re.search(r"apk add --no-cache[^&]*\bsu-exec\b", dockerfile)


# ── Staging / production volumes ─────────────────────────────────────────────


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("stack", STACKS)
def test_app_and_worker_share_the_storage_volume(client, stack, db, queue):
    compose = _compose(_get_project(client, db=db, queue=queue), stack)
    volume = "storage_data"

    assert volume in compose["volumes"]
    for service in ("app", "worker"):
        assert f"{volume}:{STORAGE_MOUNT}" in compose["services"][service]["volumes"]


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("stack", STACKS)
def test_the_sync_queue_still_persists_storage_for_the_app(client, stack, db):
    compose = _compose(_get_project(client, db=db, queue="sync"), stack)

    assert "worker" not in compose["services"]
    assert f"storage_data:{STORAGE_MOUNT}" in compose["services"]["app"]["volumes"]


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_sqlite_lives_on_a_volume_shared_by_app_and_worker(client, stack, queue):
    compose = _compose(_get_project(client, db="sqlite", queue=queue), stack)
    volume = "sqlite_data"

    assert compose["x-app-environment"]["DB_DATABASE"] == SQLITE_FILE
    assert volume in compose["volumes"]
    for service in ("app", "worker"):
        assert f"{volume}:{SQLITE_MOUNT}" in compose["services"][service]["volumes"]
    assert not any(name.startswith("db_data") for name in compose["volumes"])


@pytest.mark.parametrize("stack", STACKS)
def test_the_sqlite_mount_is_a_new_subdirectory_so_it_does_not_hide_the_migrations(client, stack):
    compose = _compose(_get_project(client, db="sqlite"), stack)

    mounts = [v.split(":")[1] for v in compose["services"]["app"]["volumes"]]
    assert SQLITE_MOUNT in mounts
    assert "/var/www/html/database" not in mounts
    assert str(Path(compose["x-app-environment"]["DB_DATABASE"]).parent) == SQLITE_MOUNT


@pytest.mark.parametrize("db", ["mysql", "postgres"])
@pytest.mark.parametrize("stack", STACKS)
def test_server_databases_keep_their_own_volume_and_no_sqlite_one(client, stack, db):
    compose = _compose(_get_project(client, db=db), stack)

    assert set(compose["volumes"]) == {"storage_data", "db_data"}
    assert "sqlite" not in yaml.safe_dump(compose)
    for service in ("app", "worker"):
        assert not any(SQLITE_MOUNT in v for v in compose["services"][service]["volumes"])


def test_stage_and_prod_volumes_stay_apart_through_their_project_names(client):
    archive = _get_project(client, db="sqlite", queue="redis")
    stage = _compose(archive, "docker-compose.stage.yml")
    prod = _compose(archive, "docker-compose.prod.yml")

    # Compose prefixes volume names with the project name, so the names themselves
    # no longer carry the environment: only the project does.
    assert set(stage["volumes"]) == set(prod["volumes"])
    assert stage["name"] != prod["name"]


def test_dev_stack_keeps_bind_mounted_storage_and_database(client):
    compose = _compose(_get_project(client), "docker-compose.yml")

    assert "volumes" not in compose or "storage_data" not in " ".join(compose["volumes"])
    assert compose["x-app-environment"]["DB_DATABASE"] == "/var/www/html/database/database.sqlite"
    assert compose["services"]["app"]["volumes"] == [".:/var/www/html"]


# ── Prod entrypoint ──────────────────────────────────────────────────────────


def _script(client, tmp_path: Path, **params) -> str:
    path = tmp_path / "prod.entrypoint.sh"
    path.write_text(_get_project(client, **params).read(ROOT + "docker/prod.entrypoint.sh").decode())
    return str(path)


def _fake_commands(tmp_path: Path) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("su-exec", "php", "find", "chown", "php-fpm", "nginx"):
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*"\n')
        fake.chmod(0o755)
    return str(bin_dir)


def _boot(client, tmp_path: Path, *args: str, env: dict | None = None, **params) -> subprocess.CompletedProcess:
    """Runs the rendered entrypoint in ``tmp_path`` (the project root) with the fake commands first on PATH."""
    script = _script(client, tmp_path, **params)
    bin_dir = _fake_commands(tmp_path)
    project = tmp_path / "app"
    project.mkdir(exist_ok=True)
    result = subprocess.run(
        ["sh", script, *args],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/bin:/usr/bin", **(env or {})},
        cwd=project,
    )
    assert result.returncode == 0, result.stderr
    return result


@pytest.mark.parametrize("args", [(), ("php", "artisan", "queue:work")], ids=["web", "worker"])
@pytest.mark.parametrize("db", DBS)
def test_every_boot_creates_the_directories_laravel_writes_to(client, tmp_path, db, args):
    _boot(client, tmp_path, *args, db=db)

    for directory in STORAGE_DIRS:
        assert (tmp_path / "app" / directory).is_dir(), directory


@pytest.mark.parametrize("args", [(), ("php", "artisan", "queue:work")], ids=["web", "worker"])
@pytest.mark.parametrize("db", DBS)
def test_every_boot_hands_only_mismatched_files_to_www_data(client, tmp_path, db, args):
    result = _boot(client, tmp_path, *args, db=db)

    assert CHOWN_MISMATCHED in result.stdout.splitlines()


@pytest.mark.parametrize("args", [(), ("php", "artisan", "queue:work")], ids=["web", "worker"])
def test_the_storage_link_is_created_when_missing(client, tmp_path, args):
    result = _boot(client, tmp_path, *args)

    assert "php artisan storage:link --no-interaction" in result.stdout.splitlines()


@pytest.mark.parametrize("args", [(), ("php", "artisan", "queue:work")], ids=["web", "worker"])
def test_the_storage_link_is_left_alone_when_it_already_exists(client, tmp_path, args):
    project = tmp_path / "app"
    (project / "public").mkdir(parents=True)
    (project / "public" / "storage").symlink_to(project / "storage" / "app" / "public")

    result = _boot(client, tmp_path, *args)

    assert "storage:link" not in result.stdout


def test_web_mode_migrates_as_www_data_then_starts_php_fpm_and_nginx(client, tmp_path):
    lines = _boot(client, tmp_path, db="mysql").stdout.splitlines()

    migrate = lines.index("su-exec www-data php artisan migrate --force --no-interaction")
    assert lines.index(CHOWN_MISMATCHED) < migrate < lines.index("php-fpm -D") < lines.index('nginx -g daemon off;')
    assert not any(line.startswith("php artisan migrate") for line in lines)


def test_sqlite_web_mode_migrates_as_www_data(client, tmp_path):
    lines = _boot(client, tmp_path, db="sqlite").stdout.splitlines()

    assert "su-exec www-data php artisan migrate --force --no-interaction" in lines
    assert not any(line.startswith("php artisan migrate") for line in lines)


@pytest.mark.parametrize("args", [(), ("php", "artisan", "queue:work")], ids=["web", "worker"])
def test_sqlite_boot_creates_the_database_and_its_directory_for_www_data(client, tmp_path, args):
    database = tmp_path / "data" / "database.sqlite"

    result = _boot(client, tmp_path, *args, db="sqlite", env={"DB_DATABASE": str(database)})

    assert database.is_file()
    assert f"chown www-data:www-data {database.parent} {database}" in result.stdout.splitlines()


def test_sqlite_boot_without_db_database_uses_laravels_default_path(client, tmp_path):
    result = _boot(client, tmp_path, db="sqlite")

    assert (tmp_path / "app" / "database" / "database.sqlite").is_file()
    assert "chown www-data:www-data database database/database.sqlite" in result.stdout.splitlines()


@pytest.mark.parametrize("db", ["mysql", "postgres"])
def test_server_database_boot_leaves_no_sqlite_file_behind(client, tmp_path, db):
    result = _boot(client, tmp_path, db=db, env={"DB_DATABASE": "laravel"})

    assert not list(tmp_path.rglob("*.sqlite"))
    assert not any(line.startswith("chown") for line in result.stdout.splitlines())


@pytest.mark.parametrize("db", DBS)
def test_worker_mode_prepares_storage_then_execs_the_command_as_www_data(client, tmp_path, db):
    lines = _boot(client, tmp_path, "php", "artisan", "queue:work", "--sleep=3", db=db).stdout.splitlines()

    assert lines[-1] == "su-exec www-data php artisan queue:work --sleep=3"
    assert lines.index("php artisan storage:link --no-interaction") < len(lines) - 1
    assert lines.index(CHOWN_MISMATCHED) < len(lines) - 1
    assert not any("migrate" in line or line.startswith(("php-fpm", "nginx")) for line in lines)


@pytest.mark.parametrize("db", DBS)
def test_the_entrypoint_documents_both_usages(client, tmp_path, db):
    header = open(_script(client, tmp_path, db=db)).read().split("set -e")[0]

    assert "sh docker/prod.entrypoint.sh\n" in header
    assert "sh docker/prod.entrypoint.sh <command" in header
    assert "www-data" in header


# ── Dev entrypoint ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
def test_dev_entrypoint_links_public_storage_after_composer_install(client, db):
    script = _get_project(client, db=db).read(ROOT + "docker/dev.entrypoint.sh").decode()
    link = "[ -L public/storage ] || php artisan storage:link --no-interaction"

    assert link in script
    assert script.index("composer install") < script.index(link) < script.index("php artisan migrate")


# ── README ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
def test_readme_documents_the_persistent_volumes(client, db):
    readme = _get_project(client, db=db).read(ROOT + "README.md").decode()

    assert "## Storage and persistent data" in readme
    assert "| `storage_data` | `/var/www/html/storage/app` |" in readme
    assert ("`sqlite_data`" in readme) == (db == "sqlite")
    assert ("`db_data`" in readme) == (db != "sqlite")
    assert "demo-prod_storage_data" in readme  # Compose prefixes the project name
    assert "_data_<env>" not in readme
