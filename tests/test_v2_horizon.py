"""
Exercises ``GET /v2/new-inertia?horizon=true`` end to end with ``_run`` replaced
by a recorder (no Composer, PHP or npm needed), so the template context is
exactly the one the handler builds.

``horizon`` is ``false`` by default. Horizon needs Redis, so ``horizon=true`` with
any ``queue`` other than ``redis`` is a ``422``. With it, generation installs
``laravel/horizon`` and every stack's ``worker`` runs ``php artisan horizon``
instead of ``queue:listen`` / ``queue:work`` (stage/prod through the entrypoint's
worker mode, so it runs as ``www-data``).
"""

import io
import re
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from services import installer_service

ROOT = "demo/"
DBS = ["mysql", "postgres", "sqlite"]
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


def _get_project(client: TestClient, **params) -> zipfile.ZipFile:
    params = {"app_name": "demo", "db": "sqlite", "queue": "redis", "horizon": "true", **params}
    response = client.get("/v2/new-inertia", params=params)
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _compose(archive: zipfile.ZipFile, name: str) -> dict:
    return yaml.safe_load(archive.read(ROOT + name))


def _index(calls, *prefix):
    return next(i for i, c in enumerate(calls) if c.cmd[: len(prefix)] == list(prefix))


def _is_horizon(call: Call) -> bool:
    return "laravel/horizon" in call.cmd or call.cmd[:3] == ["php", "artisan", "horizon:install"]


# ── Query parameter ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("queue", ["database", "sync"])
def test_horizon_without_the_redis_queue_is_rejected(client, calls, queue):
    response = client.get("/v2/new-inertia", params={"queue": queue, "horizon": "true"})

    assert response.status_code == 422
    assert response.json() == {"detail": "horizon=true requires queue=redis."}
    assert calls == []  # rejected before anything is generated


def test_horizon_alone_is_rejected_because_the_default_queue_is_database(client):
    response = client.get("/v2/new-inertia", params={"horizon": "true"})

    assert response.status_code == 422
    assert response.json()["detail"] == "horizon=true requires queue=redis."


@pytest.mark.parametrize("queue", ["database", "redis", "sync"])
def test_horizon_false_is_accepted_with_any_queue(client, queue):
    _get_project(client, queue=queue, horizon="false")


def test_horizon_is_documented_in_the_openapi_schema(client):
    parameters = client.get("/openapi.json").json()["paths"]["/v2/new-inertia"]["get"]["parameters"]
    horizon = next(p for p in parameters if p["name"] == "horizon")

    assert horizon["schema"]["default"] is False
    assert "queue=redis" in horizon["description"]


# ── Project generation ───────────────────────────────────────────────────────


def test_horizon_is_installed_without_scripts_then_discovered_then_set_up(client, calls):
    _get_project(client)

    require = _index(calls, "composer", "require", "laravel/horizon")
    assert calls[require].cmd == [
        "composer",
        "require",
        "laravel/horizon",
        "--no-interaction",
        "--no-scripts",
        # The image has both extensions; the installer host may not.
        "--ignore-platform-req=ext-pcntl",
        "--ignore-platform-req=ext-posix",
    ]
    discover = next(
        i for i, c in enumerate(calls) if i > require and c.cmd[:3] == ["php", "artisan", "package:discover"]
    )
    assert calls[discover].cmd == ["php", "artisan", "package:discover", "--ansi"]
    install = _index(calls, "php", "artisan", "horizon:install")
    assert calls[install].cmd == ["php", "artisan", "horizon:install", "--no-interaction"]
    assert require < discover < install


def test_horizon_is_installed_after_predis_and_before_chisel(client, calls):
    """Chisel's ``install:features`` deletes itself, so nothing may fire it earlier (see ``--no-scripts``)."""
    _get_project(client)

    assert _index(calls, "composer", "require", "predis/predis") < _index(
        calls, "composer", "require", "laravel/horizon"
    )
    assert _index(calls, "php", "artisan", "horizon:install") < _index(
        calls, "php", "artisan", "install:features"
    )


def test_horizon_with_pest_keeps_every_composer_mutation_scriptless(client, calls):
    _get_project(client, testing_framework="pest")

    mutations = [c for c in calls if c.cmd[0] == "composer" and c.cmd[1] in {"remove", "require"}]
    assert any("laravel/horizon" in c.cmd for c in mutations)
    assert all("--no-scripts" in c.cmd for c in mutations)


@pytest.mark.parametrize("queue", ["database", "redis", "sync"])
def test_no_horizon_by_default(client, calls, queue):
    _get_project(client, queue=queue, horizon="false")

    assert not any(_is_horizon(c) for c in calls)


def test_horizon_defaults_to_false(client, calls):
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "db": "sqlite", "queue": "redis"})

    assert response.status_code == 200
    assert not any(_is_horizon(c) for c in calls)


# ── Dockerfile ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
def test_dockerfile_comment_mentions_pcntl_and_horizon(client, db):
    dockerfile = _get_project(client, db=db).read(ROOT + "Dockerfile").decode()

    comment = re.search(r"^#[^\n]*pcntl[^\n]*(?:\n#[^\n]*)*", dockerfile, re.MULTILINE)
    assert comment and "Horizon" in comment.group(0)
    # The comment is above, and does not replace, the extension that is installed.
    assert comment.end() < dockerfile.index("docker-php-ext-install")
    assert "pcntl" in re.search(r"^\s*docker-php-ext-install .*$", dockerfile, re.MULTILINE).group(0).split()


def test_dockerfile_comment_does_not_mention_horizon_without_it(client):
    dockerfile = _get_project(client, queue="redis", horizon="false").read(ROOT + "Dockerfile").decode()

    assert "Horizon" not in dockerfile
    assert "pcntl lets queue workers handle signals" in dockerfile


# ── Dev stack ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
def test_dev_worker_runs_horizon_after_waiting_for_vendor(client, db):
    services = _compose(_get_project(client, db=db), "docker-compose.yml")["services"]
    command = services["worker"]["command"]

    assert "until [ -f vendor/autoload.php ]" in command
    assert 'exec php artisan horizon"' in command
    assert "queue:listen" not in command and "queue:work" not in command
    assert command.index("vendor/autoload.php") < command.index("php artisan horizon")


def test_dev_worker_keeps_sharing_the_app_image_and_waiting_for_redis(client):
    services = _compose(_get_project(client), "docker-compose.yml")["services"]
    app_service, worker = services["app"], services["worker"]

    assert "build" not in worker
    assert worker["image"] == app_service["image"]
    assert worker["pull_policy"] == "never"
    assert worker["environment"] == app_service["environment"]
    assert worker["depends_on"]["redis"] == {"condition": "service_healthy"}


def test_dev_worker_without_horizon_still_listens(client):
    command = _compose(_get_project(client, horizon="false"), "docker-compose.yml")["services"]["worker"][
        "command"
    ]

    assert "exec php artisan queue:listen --tries=1 --timeout=0" in command
    assert "horizon" not in command


# ── Staging / production stacks ──────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
def test_prod_worker_runs_horizon_through_the_entrypoint_worker_mode(client, db):
    worker = _compose(_get_project(client, db=db), "docker-compose.prod.yml")["services"]["worker"]

    # The worker mode hands the command to su-exec, so Horizon runs as www-data.
    assert worker["command"] == ["sh", "docker/prod.entrypoint.sh", "worker", "php", "artisan", "horizon"]


def test_stage_worker_runs_horizon_with_the_production_supervisors(client):
    """``APP_ENV=staging`` matches no environment in config/horizon.php, so Horizon would run no workers."""
    compose = _compose(_get_project(client), "docker-compose.stage.yml")

    assert compose["x-app-environment"]["APP_ENV"] == "staging"
    assert compose["services"]["worker"]["command"] == [
        "sh",
        "docker/prod.entrypoint.sh",
        "worker",
        "php",
        "artisan",
        "horizon",
        "--environment=production",
    ]


@pytest.mark.parametrize("stack", STACKS)
def test_stack_worker_keeps_its_wiring_and_grace_period(client, stack):
    services = _compose(_get_project(client), stack)["services"]
    app_service, worker = services["app"], services["worker"]

    assert "build" not in worker
    assert worker["image"] == app_service["image"]
    assert worker["pull_policy"] == "never"
    assert worker["depends_on"]["app"] == {"condition": "service_healthy"}
    assert worker["depends_on"]["redis"] == {"condition": "service_healthy"}
    assert worker["stop_grace_period"] == "${WORKER_STOP_GRACE_PERIOD:-90s}"


@pytest.mark.parametrize("stack", STACKS)
def test_stack_without_horizon_keeps_queue_work(client, stack):
    command = _compose(_get_project(client, horizon="false"), stack)["services"]["worker"]["command"]

    assert command[-4:] == ["queue:work", "--sleep=3", "--tries=3", "--max-time=3600"]
    assert not any("horizon" in part for part in command)


@pytest.mark.parametrize("stack", ALL_STACKS)
def test_no_stack_mentions_horizon_without_it(client, stack):
    text = _get_project(client, horizon="false").read(ROOT + stack).decode()

    assert "horizon" not in text.lower()


# ── README ───────────────────────────────────────────────────────────────────


def test_readme_links_the_dashboard_and_explains_who_can_open_it(client):
    readme = _get_project(client, app_port="9090").read(ROOT + "README.md").decode()

    assert "(http://localhost:9090/horizon)" in readme
    assert "only reachable by users allowed by the `viewHorizon` gate" in readme
    assert "app/Providers/HorizonServiceProvider.php" in readme
    assert "| Horizon | yes |" in readme


def test_readme_says_the_grace_period_must_exceed_the_longest_job_timeout(client):
    readme = _get_project(client).read(ROOT + "README.md").decode()

    assert "WORKER_STOP_GRACE_PERIOD" in readme
    assert "Must exceed the longest job timeout" in readme
    assert "`php artisan horizon --environment=production`" in readme


def test_readme_without_horizon_has_no_horizon_section(client):
    readme = _get_project(client, horizon="false").read(ROOT + "README.md").decode()

    assert "Horizon" not in readme
    assert "horizon" not in readme
    assert "queue:listen" in readme
