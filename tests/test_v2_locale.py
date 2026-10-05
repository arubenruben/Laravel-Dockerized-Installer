"""
``locale`` of ``GET /v2/new-inertia``: the app's default locale.

The generated ``.env`` gets ``APP_LOCALE`` and ``APP_FALLBACK_LOCALE`` set to it. The
staging/production image has no ``.env``, so their Compose environment sets the same two
variables as ``${APP_LOCALE:-<locale>}`` / ``${APP_FALLBACK_LOCALE:-<locale>}``: the
generated locale wins over ``config/app.php``'s ``en`` default, and a deploy can still
override it. The ``/v1`` flows do not take the option and keep Laravel's ``en``.

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

STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]
LOCALES = ["en", "pt", "pt_BR", "fil", "zh_CN", "de"]

REQUIRED = {
    "APP_KEY": "base64:abc",
    "APP_URL": "https://app.example.com",
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


def _environment(archive: zipfile.ZipFile, stack: str) -> dict:
    return yaml.safe_load(_text(archive, stack))["x-app-environment"]


def _dotenv(archive: zipfile.ZipFile, name: str) -> dict[str, str]:
    lines = _text(archive, name).splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


# ── Validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "Portuguese",
        "PT",
        "pT",
        "p",
        "ptbr",
        "pt-BR",
        "pt_br",
        "pt_B",
        "pt_BRA",
        "pt_",
        "_BR",
        "pt BR",
        "pt\nen",
        "pt_BR\n",
        "português",
        "p1",
        "",
    ],
)
def test_invalid_locales_are_rejected(client, value):
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "locale": value})

    assert response.status_code == 422


@pytest.mark.parametrize("value", LOCALES)
def test_valid_locales_are_accepted(client, value):
    archive = _get_project(client, locale=value)

    assert _dotenv(archive, ".env")["APP_LOCALE"] == value


def test_the_parameter_is_optional_defaults_to_en_and_is_documented(client):
    spec = client.get("/openapi.json").json()
    parameters = spec["paths"]["/v2/new-inertia"]["get"]["parameters"]
    locale = next(p for p in parameters if p["name"] == "locale")

    assert locale["required"] is False
    assert locale["schema"]["default"] == "en"
    assert locale["schema"]["pattern"] == r"^[a-z]{2,3}(_[A-Z]{2})?$"
    assert "APP_LOCALE" in locale["description"]


# ── Generated .env ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [".env", ".env.docker"])
def test_dotenv_sets_both_variables_to_the_locale(client, name):
    dotenv = _dotenv(_get_project(client, locale="pt_BR"), name)

    assert dotenv["APP_LOCALE"] == "pt_BR"
    assert dotenv["APP_FALLBACK_LOCALE"] == "pt_BR"


def test_dotenv_defaults_to_en(client):
    dotenv = _dotenv(_get_project(client), ".env")

    assert dotenv["APP_LOCALE"] == "en"
    assert dotenv["APP_FALLBACK_LOCALE"] == "en"


def test_dotenv_defines_each_variable_once_next_to_the_other_app_settings(client):
    text = _text(_get_project(client, locale="pt"), ".env")

    assert text.count("APP_LOCALE=") == 1
    assert text.count("APP_FALLBACK_LOCALE=") == 1
    assert "APP_URL=http://localhost:8080\nAPP_LOCALE=pt\nAPP_FALLBACK_LOCALE=pt\n\nLOG_CHANNEL=stack" in text


# ── Staging and production ───────────────────────────────────────────────────


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("stack", STACKS)
def test_the_stacks_default_to_the_locale_and_let_a_deploy_override_it(client, stack, locale):
    environment = _environment(_get_project(client, locale=locale), stack)

    assert environment["APP_LOCALE"] == f"${{APP_LOCALE:-{locale}}}"
    assert environment["APP_FALLBACK_LOCALE"] == f"${{APP_FALLBACK_LOCALE:-{locale}}}"


@pytest.mark.parametrize("stack", STACKS)
def test_the_stacks_default_to_en_without_the_option(client, stack):
    environment = _environment(_get_project(client), stack)

    assert environment["APP_LOCALE"] == "${APP_LOCALE:-en}"
    assert environment["APP_FALLBACK_LOCALE"] == "${APP_FALLBACK_LOCALE:-en}"


@pytest.mark.parametrize("queue", ["sync", "database", "redis"])
@pytest.mark.parametrize("stack", STACKS)
def test_app_and_worker_get_the_locale_from_the_shared_environment(client, stack, queue):
    compose = yaml.safe_load(_text(_get_project(client, locale="pt", queue=queue), stack))

    for name in ("app", "worker"):
        if name in compose["services"]:
            assert compose["services"][name]["environment"]["APP_LOCALE"] == "${APP_LOCALE:-pt}", name


def test_the_dev_stack_does_not_set_the_locale(client):
    # The dev containers read APP_LOCALE from the project's .env.
    assert "LOCALE" not in _text(_get_project(client, locale="pt"), "docker-compose.yml")


# ── README ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("locale", ["pt", "pt_BR", "en"])
def test_readme_shows_the_locale_in_the_configuration_table(client, locale):
    readme = _text(_get_project(client, locale=locale), "README.md")

    assert f"| Locale | `{locale}` (`APP_LOCALE` and `APP_FALLBACK_LOCALE`;" in readme
    table = readme.split("## Project configuration")[1].split("\n## ")[0]
    assert table.count("| Locale |") == 1


# ── The /v1 flows are unchanged ──────────────────────────────────────────────


def test_the_shared_env_template_adds_no_locale_without_one_in_the_context():
    # `.env.docker.j2` is shared with the /v1 flows, which pass no `locale`: nothing is
    # added for them, so Laravel's own `en` default stays in charge.
    template = installer_service._jinja_env.get_template(".env.docker.j2")
    context = {"app_name": "demo", "app_port": "8000", "db": "sqlite"}

    assert "LOCALE" not in template.render(context)
    assert "APP_LOCALE=pt\n" in template.render({**context, "locale": "pt"})


@pytest.mark.parametrize("path", ["/v1/new-inertia", "/v1/release"])
def test_v1_has_no_locale_parameter(client, path):
    parameters = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]

    assert "locale" not in {p["name"] for p in parameters}


def test_v1_output_has_no_locale_and_a_locale_param_is_ignored(client):
    for params in ({}, {"locale": "pt"}):
        response = client.get("/v1/new-inertia", params={"app_name": "demo", **params})
        assert response.status_code == 200, response.text
        archive = zipfile.ZipFile(io.BytesIO(response.content))

        for name in (".env", ".env.docker", "docker-compose.yml"):
            assert "LOCALE" not in _text(archive, name), (params, name)


# ── Real `docker compose config` ─────────────────────────────────────────────


def _compose_config(tmp_path, text: str, env: dict[str, str], *extra: str, env_file: str = os.devnull):
    """Run ``docker compose config`` on ``text`` with a clean shell and, by default, no env file."""
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode:
        pytest.skip("docker compose is not installed")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(text)
    return subprocess.run(
        ["docker", "compose", "--env-file", env_file, "-f", str(compose_file), "config", *extra],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **env},
    )


def _resolved(tmp_path, client, stack, env=None, **params) -> dict:
    text = _text(_get_project(client, **params), stack)
    result = _compose_config(tmp_path, text, {**REQUIRED, **(env or {})}, "--format", "json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["services"]["app"]["environment"]


@pytest.mark.parametrize("stack", STACKS)
def test_config_uses_the_generated_locale_when_nothing_is_set(client, tmp_path, stack):
    environment = _resolved(tmp_path, client, stack, locale="pt_BR")

    assert environment["APP_LOCALE"] == "pt_BR"
    assert environment["APP_FALLBACK_LOCALE"] == "pt_BR"


def test_config_lets_the_deploy_override_the_locale(client, tmp_path):
    environment = _resolved(tmp_path, client, "docker-compose.prod.yml", {"APP_LOCALE": "en"}, locale="pt")

    assert environment["APP_LOCALE"] == "en"
    # Each variable is overridden on its own.
    assert environment["APP_FALLBACK_LOCALE"] == "pt"


def test_config_lets_the_deploy_override_the_fallback_locale(client, tmp_path):
    environment = _resolved(
        tmp_path, client, "docker-compose.stage.yml", {"APP_FALLBACK_LOCALE": "es"}, locale="pt"
    )

    assert environment["APP_LOCALE"] == "pt"
    assert environment["APP_FALLBACK_LOCALE"] == "es"


def test_config_lets_an_env_file_override_the_locale(client, tmp_path):
    text = _text(_get_project(client, locale="pt"), "docker-compose.prod.yml")
    env_file = tmp_path / "deploy.env"
    env_file.write_text("APP_LOCALE=es\n")

    result = _compose_config(tmp_path, text, REQUIRED, "--format", "json", env_file=str(env_file))

    assert result.returncode == 0, result.stderr
    environment = json.loads(result.stdout)["services"]["app"]["environment"]
    assert (environment["APP_LOCALE"], environment["APP_FALLBACK_LOCALE"]) == ("es", "pt")
