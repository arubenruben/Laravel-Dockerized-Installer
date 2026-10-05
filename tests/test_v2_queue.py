"""
Exercises ``GET /v2/new-inertia`` end to end with ``_run`` replaced by a
recorder (no Composer, PHP or npm needed), so the template context is exactly
the one the handler builds.

``queue`` is ``database`` by default, ``redis``, or ``sync`` (no queue). With
``database`` or ``redis`` every stack gets a ``worker`` that reuses the app image
(so the image is built once) and processes queued jobs; stage/prod run it through
the entrypoint's worker mode so it runs as ``www-data``. ``redis`` also adds a
Redis service and installs ``predis/predis``. ``sync`` has neither the worker nor
Redis: jobs run inline, as before.
"""

import io
import os
import re
import subprocess
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from services import installer_service

ROOT = "demo/"
DBS = ["mysql", "postgres", "sqlite"]
QUEUES = ["database", "redis"]  # the ones with a worker
ALL_QUEUES = [*QUEUES, "sync"]
STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]
ALL_STACKS = ["docker-compose.yml", *STACKS]


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


def _get_project(client: TestClient, version: str = "v2", **params) -> zipfile.ZipFile:
    response = client.get(f"/{version}/new-inertia", params={"app_name": "demo", "db": "sqlite", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _compose(archive: zipfile.ZipFile, name: str) -> dict:
    return yaml.safe_load(archive.read(ROOT + name))


def _env_file(archive: zipfile.ZipFile) -> dict[str, str]:
    lines = archive.read(ROOT + ".env").decode().splitlines()
    return dict(line.split("=", 1) for line in lines if re.match(r"^\w+=", line))


def _index(calls, *prefix):
    return next(i for i, c in enumerate(calls) if c.cmd[: len(prefix)] == list(prefix))


# ── Query parameter ──────────────────────────────────────────────────────────


def test_queue_defaults_to_database(client):
    archive = _get_project(client)

    assert _compose(archive, "docker-compose.yml")["services"]["app"]["environment"][
        "QUEUE_CONNECTION"
    ] == "database"


@pytest.mark.parametrize("queue", ["none", "sqs", "", "REDIS"])
def test_unknown_queue_is_rejected(client, queue):
    response = client.get("/v2/new-inertia", params={"queue": queue})

    assert response.status_code == 422


# ── Dockerfile ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("queue", ALL_QUEUES)
@pytest.mark.parametrize("db", DBS)
def test_dockerfile_installs_pcntl_for_every_database(client, db, queue):
    dockerfile = _get_project(client, db=db, queue=queue).read(ROOT + "Dockerfile").decode()

    installs = re.findall(r"^\s*docker-php-ext-install .*$", dockerfile, re.MULTILINE)
    assert len(installs) == 1
    assert "pcntl" in installs[0].split()


def test_dockerfile_installs_su_exec_for_the_worker_entrypoint(client):
    dockerfile = _get_project(client).read(ROOT + "Dockerfile").decode()

    assert re.search(r"apk add --no-cache[^&]*\bsu-exec\b", dockerfile)


# ── Dev stack ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("db", DBS)
def test_dev_worker_shares_the_app_image_and_environment(client, db, queue):
    services = _compose(_get_project(client, db=db, queue=queue), "docker-compose.yml")["services"]
    app_service, worker = services["app"], services["worker"]

    assert "build" in app_service
    assert "build" not in worker  # built once, or two parallel builds crash dockerd
    assert worker["image"] == app_service["image"]
    assert worker["pull_policy"] == "never"
    assert worker["environment"] == app_service["environment"]
    assert worker["environment"]["QUEUE_CONNECTION"] == queue
    assert "app" in worker["depends_on"]


def test_dev_worker_waits_for_vendor_then_listens(client):
    command = _compose(_get_project(client), "docker-compose.yml")["services"]["worker"]["command"]

    assert "until [ -f vendor/autoload.php ]" in command
    assert "exec php artisan queue:listen --tries=1 --timeout=0" in command
    assert command.index("vendor/autoload.php") < command.index("queue:listen")


def test_dev_database_queue_has_no_redis(client):
    compose = _compose(_get_project(client, queue="database"), "docker-compose.yml")

    assert "redis" not in compose["services"]
    assert not any(key.startswith("REDIS_") for key in compose["x-app-environment"])


@pytest.mark.parametrize("db", DBS)
def test_dev_redis_queue_adds_a_healthy_redis_the_app_waits_for(client, db):
    compose = _compose(_get_project(client, db=db, queue="redis"), "docker-compose.yml")
    services = compose["services"]

    assert services["redis"]["image"] == "redis:7-alpine"
    assert services["redis"]["healthcheck"]["test"] == ["CMD", "redis-cli", "ping"]
    assert services["app"]["depends_on"]["redis"] == {"condition": "service_healthy"}
    assert services["worker"]["depends_on"]["redis"] == {"condition": "service_healthy"}
    assert {k: v for k, v in compose["x-app-environment"].items() if k.startswith("REDIS_")} == {
        "REDIS_CLIENT": "predis",
        "REDIS_HOST": "redis",
        "REDIS_PORT": 6379,
    }


@pytest.mark.parametrize("db", ["mysql", "postgres"])
def test_dev_app_still_waits_for_the_database(client, db):
    services = _compose(_get_project(client, db=db, queue="redis"), "docker-compose.yml")["services"]

    assert services["app"]["depends_on"]["db"] == {"condition": "service_started"}


def test_dev_app_has_no_dependencies_with_sqlite_and_the_database_queue(client):
    services = _compose(_get_project(client, db="sqlite", queue="database"), "docker-compose.yml")["services"]

    assert "depends_on" not in services["app"]


# ── Staging / production stacks ──────────────────────────────────────────────


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("stack", STACKS)
def test_stack_worker_reuses_the_app_image_and_waits_for_a_healthy_app(client, stack, db, queue):
    services = _compose(_get_project(client, db=db, queue=queue), stack)["services"]
    app_service, worker = services["app"], services["worker"]

    assert "build" in app_service
    assert "build" not in worker
    assert worker["image"] == app_service["image"]
    assert worker["pull_policy"] == "never"
    assert worker["environment"] == app_service["environment"]
    assert worker["environment"]["QUEUE_CONNECTION"] == queue
    # A healthy app means migrations have run.
    assert worker["depends_on"]["app"] == {"condition": "service_healthy"}


@pytest.mark.parametrize("stack", STACKS)
def test_stack_worker_runs_queue_work_through_the_entrypoint_worker_mode(client, stack):
    worker = _compose(_get_project(client), stack)["services"]["worker"]

    assert worker["command"] == [
        "sh",
        "docker/prod.entrypoint.sh",
        "php",
        "artisan",
        "queue:work",
        "--sleep=3",
        "--tries=3",
        "--max-time=3600",
    ]


@pytest.mark.parametrize("stack", STACKS)
def test_stack_worker_grace_period_is_configurable_and_exceeds_the_job_timeout(client, stack):
    worker = _compose(_get_project(client), stack)["services"]["worker"]

    assert worker["stop_grace_period"] == "${WORKER_STOP_GRACE_PERIOD:-90s}"
    default = re.search(r":-(\d+)s\}", worker["stop_grace_period"])
    assert default and int(default.group(1)) > 60  # queue:work's default --timeout


@pytest.mark.parametrize("stack", STACKS)
def test_stack_database_queue_has_no_redis(client, stack):
    compose = _compose(_get_project(client, queue="database"), stack)

    assert "redis" not in compose["services"]
    assert "redis_data" not in " ".join(compose.get("volumes") or {})
    assert "REDIS_PASSWORD" not in yaml.safe_dump(compose)


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("stack", STACKS)
def test_stack_redis_is_password_protected_and_persistent(client, stack, db):
    compose = _compose(_get_project(client, db=db, queue="redis"), stack)
    redis = compose["services"]["redis"]
    suffix = "stage" if "stage" in stack else "prod"

    assert redis["image"] == "redis:7-alpine"
    assert redis["command"] == [
        "sh",
        "-c",
        'redis-server --requirepass "$$REDIS_PASSWORD" --save 60 1 --appendonly yes',
    ]
    assert redis["volumes"] == [f"redis_data_{suffix}:/data"]
    assert f"redis_data_{suffix}" in compose["volumes"]
    assert redis["healthcheck"]["test"][0] == "CMD-SHELL"
    assert 'redis-cli -a "$$REDIS_PASSWORD" --no-auth-warning ping' in redis["healthcheck"]["test"][1]
    assert compose["services"]["app"]["depends_on"]["redis"] == {"condition": "service_healthy"}
    assert compose["services"]["worker"]["depends_on"]["redis"] == {"condition": "service_healthy"}


@pytest.mark.parametrize("stack", STACKS)
def test_stack_redis_queue_refuses_to_start_without_a_password(client, stack):
    compose = _compose(_get_project(client, queue="redis"), stack)

    required = "${REDIS_PASSWORD:?REDIS_PASSWORD must be set}"
    assert compose["x-app-environment"]["REDIS_PASSWORD"] == required
    assert compose["services"]["redis"]["environment"]["REDIS_PASSWORD"] == required
    assert compose["x-app-environment"]["REDIS_CLIENT"] == "predis"
    assert compose["x-app-environment"]["REDIS_HOST"] == "redis"


@pytest.mark.parametrize("stack", STACKS)
def test_stack_keeps_its_database_volume_next_to_redis(client, stack):
    compose = _compose(_get_project(client, db="mysql", queue="redis"), stack)
    suffix = "stage" if "stage" in stack else "prod"

    assert set(compose["volumes"]) == {f"db_data_{suffix}", f"redis_data_{suffix}", f"storage_data_{suffix}"}


@pytest.mark.parametrize("stack", ALL_STACKS)
def test_every_stack_defines_the_shared_environment_once(client, stack):
    compose = _compose(_get_project(client, db="mysql", queue="redis"), stack)

    assert compose["services"]["app"]["environment"] == compose["x-app-environment"]
    assert compose["services"]["worker"]["environment"] == compose["x-app-environment"]


# ── Without a queue (queue=sync) ─────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("stack", ALL_STACKS)
def test_sync_has_no_worker_and_no_redis_in_any_stack(client, stack, db):
    archive = _get_project(client, db=db, queue="sync")
    compose = _compose(archive, stack)

    assert set(compose["services"]) <= {"app", "vite", "db"}
    assert compose["services"]["app"]["environment"]["QUEUE_CONNECTION"] == "sync"
    text = archive.read(ROOT + stack).decode()
    for leftover in ("worker", "redis", "REDIS", "WORKER_STOP_GRACE_PERIOD"):
        assert leftover not in text, f"{leftover!r} leaked into the sync {stack}"


@pytest.mark.parametrize("stack", STACKS)
def test_sync_stack_volumes_hold_only_storage_and_the_database(client, stack):
    compose = _compose(_get_project(client, db="mysql", queue="sync"), stack)
    suffix = "stage" if "stage" in stack else "prod"

    assert set(compose["volumes"]) == {f"db_data_{suffix}", f"storage_data_{suffix}"}


def test_sync_env_keeps_jobs_inline(client):
    env = _env_file(_get_project(client, queue="sync"))

    assert env["QUEUE_CONNECTION"] == "sync"
    assert not any(key.startswith("REDIS_") for key in env)


def test_sync_installs_no_extra_packages(client, calls):
    _get_project(client, queue="sync")

    assert not any("predis/predis" in c.cmd for c in calls)


def test_sync_readme_says_jobs_run_inline_and_lists_no_worker(client):
    readme = _get_project(client, queue="sync").read(ROOT + "README.md").decode()

    assert "| Queue | sync |" in readme
    assert "runs each job inline" in readme
    assert "| worker |" not in readme
    assert "docker compose logs -f worker" not in readme
    assert "WORKER_STOP_GRACE_PERIOD" not in readme


# ── Entrypoint worker mode ───────────────────────────────────────────────────


def _entrypoint(client, tmp_path, **params) -> str:
    script = tmp_path / "prod.entrypoint.sh"
    script.write_text(_get_project(client, **params).read(ROOT + "docker/prod.entrypoint.sh").decode())
    return str(script)


def _fake_commands(tmp_path) -> dict[str, str]:
    """An env whose PATH starts with fakes that print their command line.

    The real su-exec, chown to www-data, find and artisan need root, that user and a Laravel app.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("su-exec", "php", "find", "chown", "php-fpm", "nginx"):
        fake = bin_dir / name
        fake.write_text(f'#!/bin/sh\necho "{name} $*"\n')
        fake.chmod(0o755)
    return {"PATH": f"{bin_dir}:/bin:/usr/bin"}


@pytest.mark.parametrize("db", DBS)
def test_entrypoint_worker_mode_hands_the_command_to_su_exec_without_migrating(client, tmp_path, db):
    script = _entrypoint(client, tmp_path, db=db)

    result = subprocess.run(
        ["sh", script, "php", "artisan", "queue:work", "--sleep=3"],
        capture_output=True,
        text=True,
        env=_fake_commands(tmp_path),
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == "su-exec www-data php artisan queue:work --sleep=3"
    assert not any("migrate" in line or line.startswith(("php-fpm", "nginx")) for line in lines)


@pytest.mark.parametrize("db", DBS)
def test_entrypoint_is_valid_shell(client, tmp_path, db):
    script = _entrypoint(client, tmp_path, db=db)

    assert subprocess.run(["sh", "-n", script], capture_output=True).returncode == 0


def test_entrypoint_worker_mode_is_checked_before_the_app_startup(client, tmp_path):
    script = open(_entrypoint(client, tmp_path)).read()

    assert script.index('"$#" -gt 0') < script.index("php artisan migrate")
    assert script.index('"$#" -gt 0') < script.index("exec nginx")


# ── Generated .env ───────────────────────────────────────────────────────────


def test_env_uses_the_database_queue_by_default(client):
    env = _env_file(_get_project(client))

    assert env["QUEUE_CONNECTION"] == "database"
    assert not any(key.startswith("REDIS_") for key in env)


def test_env_points_the_redis_queue_at_the_compose_service(client):
    env = _env_file(_get_project(client, queue="redis"))

    assert env["QUEUE_CONNECTION"] == "redis"
    assert env["REDIS_CLIENT"] == "predis"
    assert env["REDIS_HOST"] == "redis"
    assert env["REDIS_PORT"] == "6379"
    assert "REDIS_PASSWORD" not in env  # dev Redis has none; stage/prod set it in Compose


@pytest.mark.parametrize("queue", ALL_QUEUES)
def test_env_docker_backup_matches_env(client, queue):
    archive = _get_project(client, queue=queue)

    assert archive.read(ROOT + ".env") == archive.read(ROOT + ".env.docker")


def test_v1_inertia_keeps_the_sync_queue(client):
    """/v1 ships no worker, so jobs must keep running inline."""
    env = _env_file(_get_project(client, version="v1"))

    assert env["QUEUE_CONNECTION"] == "sync"
    assert not any(key.startswith("REDIS_") for key in env)


# ── Project generation ───────────────────────────────────────────────────────


def test_database_queue_installs_no_extra_packages(client, calls):
    _get_project(client, queue="database")

    assert not any("predis/predis" in c.cmd for c in calls)


def test_redis_queue_requires_predis_without_scripts_then_discovers_packages(client, calls):
    _get_project(client, queue="redis")

    require = _index(calls, "composer", "require", "predis/predis")
    assert "--no-scripts" in calls[require].cmd
    discover = next(
        i for i, c in enumerate(calls) if i > require and c.cmd[:3] == ["php", "artisan", "package:discover"]
    )
    # Both before chisel runs: its install:features deletes itself, and a
    # post-update-cmd firing it with no answers would break the real step.
    assert discover < _index(calls, "php", "artisan", "install:features")


def test_redis_queue_with_pest_keeps_every_composer_mutation_scriptless(client, calls):
    _get_project(client, queue="redis", testing_framework="pest")

    mutations = [c for c in calls if c.cmd[0] == "composer" and c.cmd[1] in {"remove", "require"}]
    assert any("predis/predis" in c.cmd for c in mutations)
    assert all("--no-scripts" in c.cmd for c in mutations)


# ── README ───────────────────────────────────────────────────────────────────


def test_readme_describes_the_worker_and_queue(client):
    readme = _get_project(client).read(ROOT + "README.md").decode()

    assert "| Queue | database |" in readme
    assert "| worker |" in readme
    assert "| redis |" not in readme
    assert "REDIS_PASSWORD" not in readme


def test_readme_documents_the_redis_stack(client):
    readme = _get_project(client, queue="redis").read(ROOT + "README.md").decode()

    assert "| Queue | redis |" in readme
    assert "| redis | redis:7-alpine |" in readme
    assert "REDIS_PASSWORD" in readme
    assert "WORKER_STOP_GRACE_PERIOD" in readme
