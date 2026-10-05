"""
CPU and memory limits of the staging/production Compose stacks (``GET /v2/new-inertia``).

Every service of ``docker-compose.stage.yml`` / ``docker-compose.prod.yml`` has
``deploy.resources`` limits and reservations, so a runaway worker or a memory leak
cannot starve the database and everything else on the host. Each value reads
``<PREFIX>CPU_LIMIT``, ``<PREFIX>MEM_LIMIT``, ``<PREFIX>CPU_RESERVATION`` or
``<PREFIX>MEM_RESERVATION`` from the environment (prefix ``APP_``, ``WORKER_``,
``DB_`` or ``REDIS_``), with a default. MySQL 8 gets higher defaults than PostgreSQL
because it can exceed 512M at startup. The dev stack has no limits.

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

REQUIRED = {
    "APP_KEY": "base64:abc",
    "APP_URL": "https://app.example.com",
    "DB_PASSWORD": "db-secret",
    "DB_ROOT_PASSWORD": "root-secret",
    "REDIS_PASSWORD": "redis-secret",
}

MIB = 1024**2

VARIABLE_SUFFIXES = ["CPU_LIMIT", "MEM_LIMIT", "CPU_RESERVATION", "MEM_RESERVATION"]

# (prefix, CPU limit, memory limit, CPU reservation, memory reservation)
APP = ("APP_", "1.0", "512M", "0.25", "256M")
WORKER = ("WORKER_", "1.0", "512M", "0.25", "256M")
POSTGRES = ("DB_", "1.0", "512M", "0.25", "256M")
MYSQL = ("DB_", "1.0", "1G", "0.25", "512M")
REDIS = ("REDIS_", "0.5", "256M", "0.1", "128M")


def _expected_limits(db: str, queue: str) -> dict[str, tuple[str, str, str, str, str]]:
    """The services that get limits for these options, with their defaults."""
    services = {"app": APP}
    if queue != "sync":
        services["worker"] = WORKER
    if db != "sqlite":
        services["db"] = MYSQL if db == "mysql" else POSTGRES
    if queue == "redis":
        services["redis"] = REDIS
    return services


def _resources(prefix, cpu_limit, mem_limit, cpu_reservation, mem_reservation) -> dict:
    return {
        "limits": {
            "cpus": f"${{{prefix}CPU_LIMIT:-{cpu_limit}}}",
            "memory": f"${{{prefix}MEM_LIMIT:-{mem_limit}}}",
        },
        "reservations": {
            "cpus": f"${{{prefix}CPU_RESERVATION:-{cpu_reservation}}}",
            "memory": f"${{{prefix}MEM_RESERVATION:-{mem_reservation}}}",
        },
    }


class _ResetLoader(yaml.SafeLoader):
    """Reads the Compose ``!reset`` tag, which plain YAML does not know."""


_ResetLoader.add_constructor("!reset", lambda loader, node: "!reset")


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


def _readme_override(readme: str) -> str:
    """The override file the README's workaround section shows."""
    section = readme.split("### Hosts that cannot apply limits")[1]
    match = re.search(r"```yaml\n(.*?)```", section, re.S)
    assert match, "the README shows no override file"
    return match.group(1)


# ── The limits in the rendered stacks ────────────────────────────────────────


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
@pytest.mark.parametrize("stack", STACKS)
def test_every_service_has_limits_and_reservations_with_its_own_prefix(client, stack, db, queue):
    services = _compose(_get_project(client, db=db, queue=queue), stack)["services"]
    expected = _expected_limits(db, queue)

    assert set(services) == set(expected)  # no service is left without limits
    for name, defaults in expected.items():
        assert services[name]["deploy"] == {"resources": _resources(*defaults)}, name


def test_mysql_defaults_are_higher_than_postgres(client):
    mysql = _compose(_get_project(client, db="mysql"), "docker-compose.prod.yml")["services"]["db"]
    postgres = _compose(_get_project(client, db="postgres"), "docker-compose.prod.yml")["services"]["db"]

    assert mysql["deploy"]["resources"]["limits"]["memory"] == "${DB_MEM_LIMIT:-1G}"
    assert mysql["deploy"]["resources"]["reservations"]["memory"] == "${DB_MEM_RESERVATION:-512M}"
    assert postgres["deploy"]["resources"]["limits"]["memory"] == "${DB_MEM_LIMIT:-512M}"
    assert postgres["deploy"]["resources"]["reservations"]["memory"] == "${DB_MEM_RESERVATION:-256M}"


def test_worker_and_app_are_limited_independently(client):
    services = _compose(_get_project(client, queue="database"), "docker-compose.prod.yml")["services"]
    names = lambda service: set(re.findall(r"\$\{(\w+)", json.dumps(service["deploy"])))  # noqa: E731

    assert names(services["app"]) == {f"APP_{suffix}" for suffix in VARIABLE_SUFFIXES}
    assert names(services["worker"]) == {f"WORKER_{suffix}" for suffix in VARIABLE_SUFFIXES}


def test_dev_stack_has_no_limits(client):
    services = _compose(_get_project(client, db="mysql", queue="redis"), "docker-compose.yml")["services"]

    assert all("deploy" not in service for service in services.values())


# ── Documentation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stack", STACKS)
def test_header_documents_the_variables_and_the_cgroup_workaround(client, stack):
    text = _text(_get_project(client, db="mysql", queue="redis"), stack)
    header = text.split("\nname:")[0]
    prose = re.sub(r"\s*\n#\s*", " ", header)  # comment lines reflowed, so wrapping doesn't matter

    assert "<PREFIX>CPU_LIMIT" in prose and "<PREFIX>MEM_LIMIT" in prose
    assert "<PREFIX>CPU_RESERVATION" in prose and "<PREFIX>MEM_RESERVATION" in prose
    assert "(APP_, WORKER_, DB_, REDIS_)" in prose
    assert "cannot enter cgroupv2 ... in threaded mode" in prose
    assert all(line.startswith("#") or not line for line in header.splitlines())


@pytest.mark.parametrize(("db", "queue"), [("sqlite", "sync"), ("postgres", "database")])
def test_header_lists_only_the_prefixes_of_services_that_exist(client, db, queue):
    header = _text(_get_project(client, db=db, queue=queue), "docker-compose.prod.yml").split("\nname:")[0]
    expected = ", ".join(defaults[0] for defaults in _expected_limits(db, queue).values())

    assert f"({expected})" in header


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
def test_readme_table_lists_the_defaults_of_every_limited_service(client, db, queue):
    readme = _text(_get_project(client, db=db, queue=queue), "README.md")
    section = readme.split("## Resource limits")[1].split("### Hosts that cannot apply limits")[0]
    rows = [line for line in section.splitlines() if line.startswith("| `")]
    expected = _expected_limits(db, queue)

    assert len(rows) == len(expected)
    for row, (prefix, *values) in zip(rows, expected.values(), strict=True):
        assert f"| `{prefix}` |" in row
        assert row.endswith("".join(f" `{value}` |" for value in values))
    for suffix in VARIABLE_SUFFIXES:
        assert f"`<PREFIX>{suffix}`" in section
    assert "APP_MEM_LIMIT=1G" in section
    assert ("MySQL 8 gets higher defaults" in section) == (db == "mysql")


@pytest.mark.parametrize("db", DBS)
@pytest.mark.parametrize("queue", QUEUES)
def test_readme_override_resets_exactly_the_limited_services(client, db, queue):
    readme = _text(_get_project(client, db=db, queue=queue), "README.md")
    override = yaml.load(_readme_override(readme), Loader=_ResetLoader)  # noqa: S506 - SafeLoader subclass

    assert override == {"services": {name: {"deploy": "!reset"} for name in _expected_limits(db, queue)}}
    assert "docker compose -f docker-compose.prod.yml -f docker-compose.no-limits.yml up -d" in readme
    assert "cannot enter cgroupv2 ... in threaded mode" in readme


# ── Real `docker compose config` ─────────────────────────────────────────────


def _compose_config(tmp_path, text: str, env: dict[str, str], *, override: str | None = None):
    """Run ``docker compose config`` on ``text`` (plus ``override``) with a clean shell and no env file."""
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode:
        pytest.skip("docker compose is not installed")
    files = ["-f", str(tmp_path / "docker-compose.yml")]
    (tmp_path / "docker-compose.yml").write_text(text)
    if override is not None:
        (tmp_path / "docker-compose.no-limits.yml").write_text(override)
        files += ["-f", str(tmp_path / "docker-compose.no-limits.yml")]
    return subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, *files, "config", "--format", "json"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **REQUIRED, **env},
    )


def _config(tmp_path, client, stack, env=None, override=None, **params) -> dict:
    archive = _get_project(client, **params)
    result = _compose_config(tmp_path, _text(archive, stack), env or {}, override=override)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _numbers(service: dict) -> tuple:
    """CPU limit, memory limit (MiB), CPU reservation, memory reservation (MiB), whatever the Compose version prints."""
    resources = service["deploy"]["resources"]
    return (
        float(resources["limits"]["cpus"]),
        int(resources["limits"]["memory"]) // MIB,
        float(resources["reservations"]["cpus"]),
        int(resources["reservations"]["memory"]) // MIB,
    )


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize(("db", "db_numbers"), [("mysql", (1, 1024, 0.25, 512)), ("postgres", (1, 512, 0.25, 256))])
def test_config_resolves_the_default_limits(client, tmp_path, stack, db, db_numbers):
    services = _config(tmp_path, client, stack, db=db, queue="redis")["services"]

    assert {name: _numbers(service) for name, service in services.items()} == {
        "app": (1, 512, 0.25, 256),
        "worker": (1, 512, 0.25, 256),
        "db": db_numbers,
        "redis": (0.5, 256, 0.1, 128),
    }


@pytest.mark.parametrize("stack", STACKS)
def test_config_overriding_one_variable_changes_only_that_service(client, tmp_path, stack):
    before = _config(tmp_path, client, stack, db="mysql", queue="redis")["services"]
    after = _config(tmp_path, client, stack, {"APP_MEM_LIMIT": "1G"}, db="mysql", queue="redis")["services"]

    assert _numbers(after["app"]) == (1, 1024, 0.25, 256)
    assert {n: _numbers(s) for n, s in after.items() if n != "app"} == {
        n: _numbers(s) for n, s in before.items() if n != "app"
    }


@pytest.mark.parametrize("stack", STACKS)
def test_config_takes_every_variable_of_every_service_from_the_environment(client, tmp_path, stack):
    values = {
        "APP_": ("2", "2G", "0.5", "1G"),
        "WORKER_": ("1.5", "768M", "0.3", "384M"),
        "DB_": ("3", "4G", "1", "2G"),
        "REDIS_": ("0.75", "320M", "0.2", "160M"),
    }  # in VARIABLE_SUFFIXES order
    env = {
        f"{prefix}{suffix}": value
        for prefix, prefix_values in values.items()
        for suffix, value in zip(VARIABLE_SUFFIXES, prefix_values, strict=True)
    }
    services = _config(tmp_path, client, stack, env, db="mysql", queue="redis")["services"]

    assert {name: _numbers(service) for name, service in services.items()} == {
        "app": (2, 2048, 0.5, 1024),
        "worker": (1.5, 768, 0.3, 384),
        "db": (3, 4096, 1, 2048),
        "redis": (0.75, 320, 0.2, 160),
    }


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize(("db", "queue"), [("mysql", "redis"), ("postgres", "database"), ("sqlite", "sync")])
def test_config_with_the_readme_override_drops_every_limit(client, tmp_path, stack, db, queue):
    readme = _text(_get_project(client, db=db, queue=queue), "README.md")
    services = _config(tmp_path, client, stack, override=_readme_override(readme), db=db, queue=queue)["services"]

    assert set(services) == set(_expected_limits(db, queue))
    assert all("deploy" not in service for service in services.values())
