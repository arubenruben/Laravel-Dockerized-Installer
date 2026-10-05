"""
``proxy_network`` of ``GET /v2/new-inertia``: attach the staging/production stacks to an
external reverse-proxy network.

Without it, ``app`` publishes ``${APP_PORT}:80`` and the stacks know nothing about a proxy.
With it, ``app`` publishes no port and joins ``default`` plus the external network under
the alias ``<app-slug>-stage`` / ``<app-slug>-prod``, which the proxy routes to
(``http://<alias>:80``). ``worker``, ``db`` and ``redis`` stay on ``default`` only. Both
stacks name the same external network, so staging and production can join it at once.

``_run`` is replaced by a recorder (no Composer, PHP or npm needed). The tests that call
``docker compose config`` are skipped when the Compose plugin is not installed.
"""

import io
import json
import os
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
ALIAS = {"docker-compose.stage.yml": "demo-stage", "docker-compose.prod.yml": "demo-prod"}

REQUIRED = {
    "APP_KEY": "base64:abc",
    "APP_URL": "https://app.example.com",
    "DB_PASSWORD": "db-secret",
    "DB_ROOT_PASSWORD": "root-secret",
    "REDIS_PASSWORD": "redis-secret",
}


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


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["bad net!", "-proxy", "_proxy", ".proxy", "proxy net", "proxy/net", "proxy:net", "réseau", "x" * 65, ""],
)
def test_invalid_network_names_are_rejected(client, value):
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "proxy_network": value})

    assert response.status_code == 422


@pytest.mark.parametrize("value", ["proxy-net", "p", "0", "Proxy_Net.1", "a-b_c.d", "x" * 64])
def test_valid_network_names_are_accepted(client, value):
    archive = _get_project(client, proxy_network=value)

    assert _compose(archive, "docker-compose.prod.yml")["networks"]["proxy"]["name"] == value


def test_the_parameter_is_optional_and_documented(client):
    parameters = client.get("/openapi.json").json()["paths"]["/v2/new-inertia"]["get"]["parameters"]
    proxy_network = next(p for p in parameters if p["name"] == "proxy_network")

    assert proxy_network["required"] is False
    schema = proxy_network["schema"]
    assert any(branch.get("maxLength") == 64 for branch in schema["anyOf"])
    assert any(branch.get("pattern") == r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$" for branch in schema["anyOf"])


# ── With proxy_network ───────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_app_publishes_no_port_and_joins_default_and_the_proxy_network(client, stack, db, queue):
    archive = _get_project(client, db=db, queue=queue, proxy_network="proxy-net")
    compose = _compose(archive, stack)

    app_service = compose["services"]["app"]
    assert "ports" not in app_service
    assert app_service["networks"] == {"default": None, "proxy": {"aliases": [ALIAS[stack]]}}
    assert compose["networks"] == {
        "default": None,
        "proxy": {"name": "proxy-net", "external": True},
    }


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_every_other_service_stays_on_the_default_network_only(client, stack, db, queue):
    services = _compose(_get_project(client, db=db, queue=queue, proxy_network="proxy-net"), stack)["services"]

    for name, service in services.items():
        if name != "app":
            assert "networks" not in service, name
            assert "ports" not in service, name


def test_the_stacks_use_different_aliases_on_the_same_external_network(client):
    archive = _get_project(client, db="mysql", queue="redis", proxy_network="proxy-net")
    stage = _compose(archive, "docker-compose.stage.yml")
    prod = _compose(archive, "docker-compose.prod.yml")

    assert stage["networks"]["proxy"] == prod["networks"]["proxy"]
    stage_alias = stage["services"]["app"]["networks"]["proxy"]["aliases"]
    prod_alias = prod["services"]["app"]["networks"]["proxy"]["aliases"]
    assert stage_alias == ["demo-stage"]
    assert prod_alias == ["demo-prod"]


@pytest.mark.parametrize("name", ["true", "null", "123", "1.5", "0x1F", "1e3", "no"])
@pytest.mark.parametrize("stack", STACKS)
def test_a_name_yaml_would_read_as_a_non_string_stays_a_string(client, stack, name):
    compose = _compose(_get_project(client, proxy_network=name), stack)

    assert compose["networks"]["proxy"]["name"] == name


@pytest.mark.parametrize("app_name", ["Minha Região", "My  App!", "7 Seas", "---", "ÅÄÖ"])
@pytest.mark.parametrize("stack", STACKS)
def test_alias_is_the_slug_plus_environment_for_any_app_name(client, stack, app_name):
    compose = _compose(_get_project(client, app_name=app_name, proxy_network="proxy-net"), stack)

    alias = compose["services"]["app"]["networks"]["proxy"]["aliases"][0]
    assert alias == compose["name"]
    assert alias.endswith("-stage" if "stage" in stack else "-prod")


@pytest.mark.parametrize("stack", STACKS)
def test_app_explains_how_the_proxy_reaches_it(client, stack):
    text = _text(_get_project(client, proxy_network="proxy-net"), stack)
    app_block = text.split("\n  app:\n")[1].split("\n  worker:\n")[0]

    assert "No published port" in app_block
    assert f"http://{ALIAS[stack]}:80" in app_block
    # Compose also registers the service name `app` on the shared network.
    assert "Not at `app`" in app_block


@pytest.mark.parametrize("stack", STACKS)
def test_header_documents_the_network_instead_of_the_port(client, stack):
    text = _text(_get_project(client, proxy_network="proxy-net"), stack)
    header = text.split("\nname:")[0]

    assert "docker network create proxy-net" in header
    assert f"http://{ALIAS[stack]}:80" in header
    assert "APP_PORT" not in header
    assert all(line.startswith("#") or not line for line in header.splitlines())


def test_the_stacks_still_differ_only_in_environment_and_name_suffixes(client):
    archive = _get_project(client, db="mysql", queue="redis", proxy_network="proxy-net")
    stage = _text(archive, "docker-compose.stage.yml")
    prod = _text(archive, "docker-compose.prod.yml")

    assert stage.replace("staging", "production").replace("stage", "prod") == prod


def test_readme_explains_the_network_and_the_aliases(client):
    readme = _text(_get_project(client, proxy_network="proxy-net"), "README.md")

    assert "## Reverse proxy and TLS" in readme
    assert "docker network create proxy-net" in readme
    assert "`demo-stage`" in readme and "http://demo-stage:80" in readme
    assert "`demo-prod`" in readme and "http://demo-prod:80" in readme
    assert "| Proxy network | `proxy-net`" in readme
    assert "Do not use `app`" in readme
    assert "`APP_PORT` has no effect" in readme
    assert "Both stacks listen on the host port" not in readme


def test_the_dev_stack_is_not_affected(client):
    without = _text(_get_project(client, queue="redis"), "docker-compose.yml")
    with_proxy = _text(_get_project(client, queue="redis", proxy_network="proxy-net"), "docker-compose.yml")

    assert with_proxy == without
    assert "proxy" not in with_proxy


# ── Without proxy_network ────────────────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_without_it_the_port_is_published_and_no_network_is_declared(client, stack, db, queue):
    text = _text(_get_project(client, db=db, queue=queue), stack)
    compose = yaml.safe_load(text)

    default_port = "8001" if "stage" in stack else "8000"
    assert compose["services"]["app"]["ports"] == [f"${{APP_PORT:-{default_port}}}:80"]
    assert "networks" not in compose
    assert all("networks" not in service for service in compose["services"].values())
    assert "proxy" not in text
    assert "external" not in text


def test_without_it_the_readme_recommends_a_tls_terminating_proxy_in_front_of_the_port(client):
    readme = _text(_get_project(client), "README.md")

    assert "## Reverse proxy and TLS" in readme
    assert "TLS-terminating reverse proxy" in readme
    assert "in front of the published port (`APP_PORT`)" in readme
    assert "Both stacks listen on the host port `APP_PORT`" in readme
    assert "docker network create" not in readme
    assert "| Proxy network |" not in readme


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


def _config(tmp_path, client, stack, **params) -> dict:
    result = _compose_config(
        tmp_path, _text(_get_project(client, **params), stack), REQUIRED, "--format", "json"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("stack", STACKS)
def test_config_attaches_only_app_to_the_external_network(client, tmp_path, stack):
    config = _config(tmp_path, client, stack, db="mysql", queue="redis", proxy_network="proxy-net")

    app_networks = config["services"]["app"]["networks"]
    assert set(app_networks) == {"default", "proxy"}
    assert app_networks["proxy"]["aliases"] == [ALIAS[stack]]
    assert "ports" not in config["services"]["app"]
    for name in ("worker", "db", "redis"):
        assert set(config["services"][name]["networks"]) == {"default"}, name
    # An external network keeps its own name: no project prefix.
    proxy = config["networks"]["proxy"]
    assert (proxy["name"], proxy["external"]) == ("proxy-net", True)


def test_config_has_both_stacks_on_the_same_network_under_different_aliases(client, tmp_path):
    stage = _config(tmp_path, client, "docker-compose.stage.yml", proxy_network="proxy-net")
    prod = _config(tmp_path, client, "docker-compose.prod.yml", proxy_network="proxy-net")

    assert stage["networks"]["proxy"]["name"] == prod["networks"]["proxy"]["name"] == "proxy-net"
    stage_alias = stage["services"]["app"]["networks"]["proxy"]["aliases"]
    prod_alias = prod["services"]["app"]["networks"]["proxy"]["aliases"]
    assert stage_alias != prod_alias
    # `default` is each project's own network, named after the project.
    assert stage["networks"]["default"]["name"] == "demo-stage_default"
    assert prod["networks"]["default"]["name"] == "demo-prod_default"


@pytest.mark.parametrize("name", ["true", "123"])
def test_config_keeps_a_name_yaml_would_misread_as_a_string(client, tmp_path, name):
    config = _config(tmp_path, client, "docker-compose.prod.yml", proxy_network=name)

    proxy = config["networks"]["proxy"]
    assert (proxy["name"], proxy["external"]) == (name, True)


def test_config_without_it_publishes_the_port_and_declares_no_external_network(client, tmp_path):
    config = _config(tmp_path, client, "docker-compose.prod.yml")

    assert config["services"]["app"]["ports"][0]["published"] == "8000"
    assert not any(network.get("external") for network in config["networks"].values())
