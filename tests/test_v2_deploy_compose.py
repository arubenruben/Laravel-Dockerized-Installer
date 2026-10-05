"""
Staging and production Compose stacks (``GET /v2/new-inertia``) come from one
deploy template, rendered twice with extra context, and can run side by side.

* Each file has its own top-level ``name:``, so the two stacks are separate Compose
  projects and never replace each other's ``app`` / ``db`` containers or share volumes.
* The image is ``${APP_IMAGE:-<slug>}:${IMAGE_TAG:-latest}``: built on the host
  (``up --build``) or pulled from a registry (``pull`` + ``up --no-build``). ``app``
  is the only service that builds; ``worker`` reuses the image with
  ``pull_policy: never``. Two services building the same image in one Compose file
  crashed BuildKit (``concurrent map iteration and map write``).
* The dev stack follows the same single-builder rule.

``_run`` is replaced by a recorder (no Composer, PHP or npm needed). The tests that
call ``docker compose config`` are skipped when the Compose plugin is not installed.
"""

import io
import json
import os
import re
import shutil
import subprocess
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from services import installer_service

DBS = ["mysql", "postgres", "sqlite"]
QUEUES = ["sync", "database", "redis"]
STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]
ALL_STACKS = ["docker-compose.yml", *STACKS]

REQUIRED = {
    "APP_KEY": "base64:abc",
    "APP_URL": "https://app.example.com",
    "DB_PASSWORD": "db-secret",
    "DB_ROOT_PASSWORD": "root-secret",
    "REDIS_PASSWORD": "redis-secret",
}

PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")  # what Compose accepts


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


def _text(archive: zipfile.ZipFile, name: str) -> str:
    root = archive.namelist()[0].split("/")[0]  # the slugified app name
    return archive.read(f"{root}/{name}").decode()


def _compose(archive: zipfile.ZipFile, name: str) -> dict:
    return yaml.safe_load(_text(archive, name))


# ── One template, rendered twice ─────────────────────────────────────────────


def test_only_one_deploy_template_remains():
    scaffold = installer_service.SCAFFOLD_DIR

    assert (scaffold / "docker-compose-inertia-deploy.yml.j2").is_file()
    assert not (scaffold / "docker-compose-inertia-stage.yml.j2").exists()
    assert not (scaffold / "docker-compose-inertia-prod.yml.j2").exists()


def test_stage_and_prod_render_the_deploy_template_with_extra_context():
    entries = {entry[1]: entry for entry in installer_service.INERTIA_SERVER_TEMPLATES_V2}

    assert entries["docker-compose.stage.yml"] == (
        "docker-compose-inertia-deploy.yml.j2",
        "docker-compose.stage.yml",
        {"deploy_env": "staging", "env_suffix": "stage"},
    )
    assert entries["docker-compose.prod.yml"] == (
        "docker-compose-inertia-deploy.yml.j2",
        "docker-compose.prod.yml",
        {"deploy_env": "production", "env_suffix": "prod"},
    )
    deploy_entries = [e for e in installer_service.INERTIA_SERVER_TEMPLATES_V2 if "deploy" in e[0]]
    assert len(deploy_entries) == 2


def test_entries_without_extra_context_still_render(client):
    # Two-element entries (everything else in the list, and the v1 flow) keep working.
    names = _get_project(client).namelist()

    assert any(name.endswith("/Dockerfile") for name in names)
    assert any(name.endswith("/docker/nginx.conf") for name in names)


def test_the_two_stacks_differ_only_in_environment_and_name_suffixes(client):
    archive = _get_project(client, db="mysql", queue="redis", horizon="false")
    stage = _text(archive, "docker-compose.stage.yml")
    prod = _text(archive, "docker-compose.prod.yml")

    # `staging` first, since `stage` is a prefix of it; 8001 is the staging default port.
    assert stage.replace("staging", "production").replace("stage", "prod").replace("8001", "8000") == prod


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
def test_app_env_is_the_only_environment_difference(client, db, queue):
    archive = _get_project(client, db=db, queue=queue)
    stage = _compose(archive, "docker-compose.stage.yml")["x-app-environment"]
    prod = _compose(archive, "docker-compose.prod.yml")["x-app-environment"]

    assert stage.pop("APP_ENV") == "staging"
    assert prod.pop("APP_ENV") == "production"
    assert stage == prod


# ── Separate Compose projects ────────────────────────────────────────────────


def test_each_stack_is_its_own_compose_project(client):
    archive = _get_project(client)

    assert _compose(archive, "docker-compose.stage.yml")["name"] == "demo-stage"
    assert _compose(archive, "docker-compose.prod.yml")["name"] == "demo-prod"


@pytest.mark.parametrize("app_name", ["Minha Região", "My  App!", "7 Seas", "---", "ÅÄÖ"])
@pytest.mark.parametrize("stack", STACKS)
def test_project_name_is_a_valid_compose_name_for_any_app_name(client, stack, app_name):
    name = _compose(_get_project(client, app_name=app_name), stack)["name"]

    assert PROJECT_NAME.match(name), name
    assert name.endswith("-stage" if "stage" in stack else "-prod")


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_stack_volume_names_carry_no_environment_suffix(client, stack, queue):
    compose = _compose(_get_project(client, db="mysql", queue=queue), stack)

    assert not any(name.endswith(("_stage", "_prod")) for name in compose["volumes"])


def test_stacks_have_distinct_container_names_and_default_ports(client):
    archive = _get_project(client, db="mysql", queue="redis")
    stage = _compose(archive, "docker-compose.stage.yml")["services"]
    prod = _compose(archive, "docker-compose.prod.yml")["services"]

    names = lambda services: {s["container_name"] for s in services.values()}  # noqa: E731
    assert names(stage).isdisjoint(names(prod))
    # Same checkout, no APP_PORT: both must be able to start.
    assert stage["app"]["ports"] == ["${APP_PORT:-8001}:80"]
    assert prod["app"]["ports"] == ["${APP_PORT:-8000}:80"]


# ── One image, one builder ───────────────────────────────────────────────────


@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_image_comes_from_app_image_and_image_tag(client, stack, queue):
    archive = _get_project(client, queue=queue)
    text = _text(archive, stack)
    services = yaml.safe_load(text)["services"]

    assert "x-app-image: &app-image" in text
    assert services["app"]["image"] == "${APP_IMAGE:-demo}:${IMAGE_TAG:-latest}"
    assert services["app"]["build"] == "."
    if queue != "sync":
        assert services["worker"]["image"] == services["app"]["image"]
        assert "build" not in services["worker"]
        assert services["worker"]["pull_policy"] == "never"


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", ALL_STACKS)
def test_exactly_one_service_builds_in_every_stack(client, stack, db, queue):
    services = _compose(_get_project(client, db=db, queue=queue), stack)["services"]

    builders = [name for name, service in services.items() if "build" in service]
    assert builders == ["app"]


def test_dev_stack_builds_one_image_and_the_worker_reuses_it(client):
    services = _compose(_get_project(client, queue="redis"), "docker-compose.yml")["services"]

    assert services["app"]["build"] == "."
    assert services["app"]["image"] == "demo-dev"
    assert services["worker"]["image"] == "demo-dev"
    assert services["worker"]["pull_policy"] == "never"
    assert "build" not in services["worker"]


# ── Documentation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stack", STACKS)
def test_header_documents_both_deploy_paths(client, stack):
    text = _text(_get_project(client), stack)
    header = text.split("\nname:")[0]

    assert f"docker compose -f {stack} up -d --build" in header
    assert "export APP_IMAGE=<registry>/<image> IMAGE_TAG=<tag>" in header
    assert f"docker compose -f {stack} pull" in header
    assert f"docker compose -f {stack} up -d --no-build" in header
    assert all(line.startswith("#") or not line for line in header.splitlines())


def test_header_names_the_environment_and_project(client):
    archive = _get_project(client)

    assert "— staging stack" in _text(archive, "docker-compose.stage.yml")
    assert "— production stack" in _text(archive, "docker-compose.prod.yml")
    assert "`demo-stage`" in _text(archive, "docker-compose.stage.yml")
    assert "`demo-prod`" in _text(archive, "docker-compose.prod.yml")


def test_readme_documents_both_deploy_paths_and_the_project_names(client):
    readme = _text(_get_project(client), "README.md")

    assert "## Staging and production" in readme
    assert "### Deploying" in readme
    assert "docker compose -f docker-compose.prod.yml up -d --build" in readme
    assert "export APP_IMAGE=<registry>/<image> IMAGE_TAG=<tag>" in readme
    assert "docker compose -f docker-compose.prod.yml pull" in readme
    assert "docker compose -f docker-compose.prod.yml up -d --no-build" in readme
    assert "`demo-stage`" in readme and "`demo-prod`" in readme
    assert "`${APP_IMAGE:-demo}:${IMAGE_TAG:-latest}`" in readme


def test_readme_says_worker_reuses_the_image_only_when_there_is_a_worker(client):
    with_worker = _text(_get_project(client, queue="redis"), "README.md")
    without = _text(_get_project(client, queue="sync"), "README.md")

    assert "`worker` reuses it" in with_worker
    assert "`worker` reuses it" not in without


# ── Real `docker compose config` ─────────────────────────────────────────────


def _compose_config(tmp_path, text: str, env: dict[str, str], *extra: str):
    """Run ``docker compose config`` on ``text`` with a clean shell and no env file."""
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode:
        pytest.skip("docker compose is not installed")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(text)
    return subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", str(compose_file), "config", *extra],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **env},
    )


def _config(tmp_path, client, stack, env=None, **params) -> dict:
    result = _compose_config(
        tmp_path, _text(_get_project(client, **params), stack), {**REQUIRED, **(env or {})}, "--format", "json"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("stack", STACKS)
def test_config_resolves_the_default_image_and_keeps_the_declared_project_name(client, tmp_path, stack):
    config = _config(tmp_path, client, stack, db="mysql", queue="redis")

    assert config["name"] == ("demo-stage" if "stage" in stack else "demo-prod")
    assert config["services"]["app"]["image"] == "demo:latest"
    assert config["services"]["worker"]["image"] == "demo:latest"
    assert "build" in config["services"]["app"]
    assert "build" not in config["services"]["worker"]
    assert config["services"]["worker"]["pull_policy"] == "never"


@pytest.mark.parametrize("stack", STACKS)
def test_config_points_app_and_worker_at_a_registry_image(client, tmp_path, stack):
    env = {"APP_IMAGE": "registry.example.com/acme/demo", "IMAGE_TAG": "v1.2.3"}
    services = _config(tmp_path, client, stack, env, queue="database")["services"]

    assert services["app"]["image"] == "registry.example.com/acme/demo:v1.2.3"
    assert services["worker"]["image"] == "registry.example.com/acme/demo:v1.2.3"


def test_config_gives_the_stacks_different_names_and_volumes(client, tmp_path):
    stage = _config(tmp_path, client, "docker-compose.stage.yml", db="postgres", queue="redis")
    prod = _config(tmp_path, client, "docker-compose.prod.yml", db="postgres", queue="redis")

    assert stage["name"] != prod["name"]
    # `config` prints the volume's real name: the project name plus the key.
    stage_volumes = {v["name"] for v in stage["volumes"].values()}
    prod_volumes = {v["name"] for v in prod["volumes"].values()}
    assert stage_volumes and stage_volumes.isdisjoint(prod_volumes)
    assert stage_volumes == {f"demo-stage_{key}" for key in ("storage_data", "db_data", "redis_data")}


def test_config_publishes_different_host_ports_unless_app_port_is_set(client, tmp_path):
    def published(stack, env=None):
        ports = _config(tmp_path, client, stack, env)["services"]["app"]["ports"]
        return ports[0]["published"]

    assert published("docker-compose.stage.yml") == "8001"
    assert published("docker-compose.prod.yml") == "8000"
    assert published("docker-compose.prod.yml", {"APP_PORT": "9000"}) == "9000"
