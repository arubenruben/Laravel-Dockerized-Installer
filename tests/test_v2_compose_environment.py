"""
Runtime environment of the staging/production Compose stacks (``GET /v2/new-inertia``).

The image has no ``.env`` (``.dockerignore`` excludes it), so everything the app
needs at runtime comes from the shared ``x-app-environment`` block of
``docker-compose.stage.yml`` / ``docker-compose.prod.yml``. Secrets use the
``${VAR:?message}`` form so ``docker compose`` refuses to start with them unset,
instead of starting with an empty string; ``APP_URL`` is required too, so links
built outside a request do not fall back to ``http://localhost``.

``_run`` is replaced by a recorder (no Composer, PHP or npm needed). The tests that
call ``docker compose config`` are skipped when the Compose plugin is not installed.
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
from services.installer_service import compose_default, yaml_quote

DBS = ["mysql", "postgres", "sqlite"]
STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]

# Every variable the stacks refuse to start without, for the options used below.
REQUIRED = {
    "APP_KEY": "base64:abc",
    "APP_URL": "https://app.example.com",
    "DB_PASSWORD": "db-secret",
    "DB_ROOT_PASSWORD": "root-secret",
    "REDIS_PASSWORD": "redis-secret",
}

# What Compose says after "is missing a value:" for each of them.
REQUIRED_MESSAGES = {
    "APP_KEY": "APP_KEY must be set",
    "APP_URL": "APP_URL must be set, e.g. https://app.example.com",
    "DB_PASSWORD": "DB_PASSWORD must be set",
    "DB_ROOT_PASSWORD": "DB_ROOT_PASSWORD must be set",
    "REDIS_PASSWORD": "REDIS_PASSWORD must be set",
}

MAIL_KEYS = [
    "MAIL_MAILER",
    "MAIL_SCHEME",
    "MAIL_HOST",
    "MAIL_PORT",
    "MAIL_USERNAME",
    "MAIL_PASSWORD",
    "MAIL_FROM_ADDRESS",
    "MAIL_FROM_NAME",
]

# Names that would break a Compose default or a YAML scalar if inserted verbatim.
AWKWARD_NAMES = [
    'Minha Região "x"',
    "Cost $5 ${B} $$ #1: - é",
    "a}b",
    "$",
    "${APP_KEY}",
    "line1\nline2",
    "x\u2028y\x7fz",
    "back\\slash 'single'",
]


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


def _stack_text(client: TestClient, stack: str, **params) -> str:
    archive = _get_project(client, **params)
    root = archive.namelist()[0].split("/")[0]  # the slugified app name
    return archive.read(f"{root}/{stack}").decode()


def _compose(client: TestClient, stack: str, **params) -> dict:
    return yaml.safe_load(_stack_text(client, stack, **params))


# ── Escaping helpers ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("my-app", "my-app"),
        ("Minha Região", "Minha Região"),
        ("Cost $5", "Cost $$5"),
        ("${APP_KEY}", "$${APP_KEY"),
        ("a}b}", "ab"),
        ("two\nlines\r\nhere", "two lines  here"),
        ("a\u2028b\u0085c", "a b c"),
    ],
)
def test_compose_default_escapes_dollars_drops_braces_and_flattens_line_breaks(value, expected):
    assert compose_default(value) == expected


@pytest.mark.parametrize("value", ["", *AWKWARD_NAMES, "${APP_NAME:-x}", "\ufeff\x85"])
def test_yaml_quote_round_trips_through_a_yaml_parser(value):
    assert yaml.safe_load("key: " + yaml_quote(value)) == {"key": value}


def test_yaml_quote_keeps_readable_characters_raw():
    assert yaml_quote('Região "x"') == '"Região \\"x\\""'


# ── Shared environment ───────────────────────────────────────────────────────


@pytest.mark.parametrize("stack", STACKS)
def test_app_and_worker_share_the_environment_by_merging_the_anchor(client, stack):
    text = _stack_text(client, stack, queue="database")
    compose = yaml.safe_load(text)

    assert text.count("<<: *app-environment") == 2
    assert compose["services"]["app"]["environment"] == compose["x-app-environment"]
    assert compose["services"]["worker"]["environment"] == compose["x-app-environment"]


@pytest.mark.parametrize("stack", STACKS)
def test_environment_carries_name_url_locale_and_mail_settings(client, stack):
    environment = _compose(client, stack)["x-app-environment"]

    assert environment["APP_NAME"] == "${APP_NAME:-demo}"
    assert environment["APP_DEBUG"] == "false"
    assert environment["APP_LOCALE"] == "${APP_LOCALE:-en}"
    assert environment["APP_FALLBACK_LOCALE"] == "${APP_FALLBACK_LOCALE:-en}"
    for key in MAIL_KEYS:
        assert environment[key].startswith(f"${{{key}:-"), key


@pytest.mark.parametrize("stack", STACKS)
def test_mail_goes_to_the_log_until_it_is_configured(client, stack):
    environment = _compose(client, stack)["x-app-environment"]

    assert environment["MAIL_MAILER"] == "${MAIL_MAILER:-log}"
    # The sender name follows APP_NAME, like Laravel's own default.
    assert environment["MAIL_FROM_NAME"] == "${MAIL_FROM_NAME:-${APP_NAME:-demo}}"


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize("db", DBS)
def test_secrets_and_app_url_must_be_set(client, stack, db):
    compose = _compose(client, stack, db=db, queue="redis")
    environment = compose["x-app-environment"]

    assert environment["APP_KEY"] == "${APP_KEY:?APP_KEY must be set}"
    assert environment["APP_URL"] == "${APP_URL:?APP_URL must be set, e.g. https://app.example.com}"
    assert environment["REDIS_PASSWORD"] == "${REDIS_PASSWORD:?REDIS_PASSWORD must be set}"
    if db == "sqlite":
        assert "DB_PASSWORD" not in environment
    else:
        assert environment["DB_PASSWORD"] == "${DB_PASSWORD:?DB_PASSWORD must be set}"


@pytest.mark.parametrize("stack", STACKS)
def test_database_containers_refuse_to_start_without_a_password(client, stack):
    mysql = _compose(client, stack, db="mysql")["services"]["db"]["environment"]
    postgres = _compose(client, stack, db="postgres")["services"]["db"]["environment"]

    assert mysql["MYSQL_PASSWORD"] == "${DB_PASSWORD:?DB_PASSWORD must be set}"
    assert mysql["MYSQL_ROOT_PASSWORD"] == "${DB_ROOT_PASSWORD:?DB_ROOT_PASSWORD must be set}"
    assert postgres["POSTGRES_PASSWORD"] == "${DB_PASSWORD:?DB_PASSWORD must be set}"


@pytest.mark.parametrize("stack", STACKS)
def test_mysql_healthcheck_reads_the_password_inside_the_container(client, stack):
    healthcheck = _compose(client, stack, db="mysql")["services"]["db"]["healthcheck"]

    # `$$` reaches the container as `$`, so the shell there expands its own variable
    # instead of Compose writing the password into the rendered command.
    assert healthcheck["test"] == ["CMD-SHELL", 'mysqladmin ping -h localhost -u laravel -p"$$MYSQL_PASSWORD"']


@pytest.mark.parametrize("stack", STACKS)
def test_sqlite_sync_stack_has_no_worker_but_keeps_the_shared_environment(client, stack):
    compose = _compose(client, stack, db="sqlite", queue="sync")

    assert "worker" not in compose["services"]
    assert compose["services"]["app"]["environment"] == compose["x-app-environment"]
    assert "DB_PASSWORD" not in compose["x-app-environment"]


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize("name", AWKWARD_NAMES)
def test_awkward_app_names_still_render_valid_yaml(client, stack, name):
    environment = _compose(client, stack, app_name=name)["x-app-environment"]

    default = compose_default(name)
    assert environment["APP_NAME"] == "${APP_NAME:-" + default + "}"
    assert environment["MAIL_FROM_NAME"] == "${MAIL_FROM_NAME:-${APP_NAME:-" + default + "}}"


@pytest.mark.parametrize("stack", STACKS)
def test_header_explains_env_files_and_how_to_check_the_result(client, stack):
    header = _stack_text(client, stack, db="mysql").split("x-app-environment:")[0]

    assert "--env-file" in header
    assert "named exactly `.env`" in header
    assert f"docker compose -f {stack} config" in header
    assert "APP_KEY, APP_URL, DB_PASSWORD" in header


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


@pytest.mark.parametrize("stack", STACKS)
def test_config_fails_in_a_clean_shell_naming_a_missing_variable(client, tmp_path, stack):
    result = _compose_config(tmp_path, _stack_text(client, stack, db="mysql", queue="redis"), {})

    # Compose reports whichever missing variable it meets first, in no fixed order.
    assert result.returncode != 0
    assert "required variable" in result.stderr
    assert "is missing a value" in result.stderr


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize("missing", ["APP_KEY", "APP_URL", "DB_PASSWORD", "DB_ROOT_PASSWORD", "REDIS_PASSWORD"])
def test_config_names_the_one_variable_that_is_missing(client, tmp_path, stack, missing):
    env = {key: value for key, value in REQUIRED.items() if key != missing}
    result = _compose_config(tmp_path, _stack_text(client, stack, db="mysql", queue="redis"), env)

    assert result.returncode != 0
    assert f"required variable {missing} is missing a value: {REQUIRED_MESSAGES[missing]}" in result.stderr


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize(("db", "queue"), [("mysql", "redis"), ("postgres", "database"), ("sqlite", "sync")])
def test_config_succeeds_with_every_required_variable_and_both_services_match(
    client, tmp_path, stack, db, queue
):
    text = _stack_text(client, stack, db=db, queue=queue)
    result = _compose_config(tmp_path, text, REQUIRED, "--format", "json")

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    environment = services["app"]["environment"]
    assert environment["APP_URL"] == "https://app.example.com"
    assert environment["MAIL_MAILER"] == "log"
    assert environment["APP_NAME"] == "demo"
    if queue != "sync":
        assert services["worker"]["environment"] == environment


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize("name", AWKWARD_NAMES)
def test_config_accepts_awkward_app_names(client, tmp_path, stack, name):
    text = _stack_text(client, stack, app_name=name)
    result = _compose_config(tmp_path, text, REQUIRED, "--format", "json")

    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)["services"]["app"]["environment"]
    # `config` prints `$` as `$$` so its output can be fed back to Compose.
    shown = environment["APP_NAME"].replace("$$", "$")
    assert shown == compose_default(name).replace("$$", "$")
    assert environment["MAIL_FROM_NAME"] == environment["APP_NAME"]


def test_config_lets_the_shell_override_the_defaults(client, tmp_path):
    env = {**REQUIRED, "APP_NAME": "Real Name", "MAIL_MAILER": "smtp", "APP_LOCALE": "pt"}
    result = _compose_config(tmp_path, _stack_text(client, "docker-compose.prod.yml"), env, "--format", "json")

    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)["services"]["app"]["environment"]
    assert environment["APP_NAME"] == "Real Name"
    assert environment["MAIL_FROM_NAME"] == "Real Name"
    assert environment["MAIL_MAILER"] == "smtp"
    assert environment["APP_LOCALE"] == "pt"
    assert environment["APP_FALLBACK_LOCALE"] == "en"
