"""
The Docker settings are merged into the starter kit's ``.env`` instead of replacing it.

``.env.docker.j2`` is a list of ``KEY=value`` overrides. The Inertia flows (``/v1`` and
``/v2``) apply it with ``_merge_env``, so the kit's own keys and comments survive and
only the Docker values change; ``.env.docker`` (it holds the ``APP_KEY``) is added to the
project's ``.gitignore``. ``/v1/release`` has no kit ``.env`` to merge into: it writes the
list as it is, with the legacy key names its older Laravel versions read.

``_run`` is replaced by a stand-in that lays down a Laravel 12 starter kit ``.env.example``
and ``.gitignore`` (no Composer, PHP or npm needed); GitHub is stubbed for ``/v1/release``.
"""

import io
import zipfile
from enum import Enum

import pytest
from fastapi.testclient import TestClient

from main import app
from services import github_service, installer_service
from services.installer_service import _gitignore_env_docker, _merge_env, dotenv_quote

KIT_ENV = """\
APP_NAME=Laravel
APP_ENV=local
APP_KEY=
APP_DEBUG=true
APP_URL=http://localhost

APP_LOCALE=en
APP_FALLBACK_LOCALE=en
APP_FAKER_LOCALE=en_US

BCRYPT_ROUNDS=12

LOG_CHANNEL=stack
LOG_STACK=single

DB_CONNECTION=sqlite
# DB_HOST=127.0.0.1
# DB_PORT=3306
# DB_DATABASE=laravel
# DB_USERNAME=root
# DB_PASSWORD=

SESSION_DRIVER=database
SESSION_LIFETIME=120

BROADCAST_CONNECTION=log
FILESYSTEM_DISK=local
QUEUE_CONNECTION=database

CACHE_STORE=database
# CACHE_PREFIX=

REDIS_CLIENT=phpredis
REDIS_HOST=127.0.0.1
REDIS_PASSWORD=null
REDIS_PORT=6379

MAIL_MAILER=log
MAIL_FROM_ADDRESS="hello@example.com"
MAIL_FROM_NAME="${APP_NAME}"

VITE_APP_NAME="${APP_NAME}"
"""

KIT_GITIGNORE = "/vendor\n/node_modules\n.env\n.env.backup\n.env.production\n"

INERTIA_PATHS = ["/v1/new-inertia", "/v2/new-inertia"]


@pytest.fixture
def client(monkeypatch):
    def fake_run(cmd, *, cwd, env, timeout, check=True):
        if cmd[:2] == ["composer", "create-project"]:
            project = installer_service.Path(cwd) / cmd[3]
            project.mkdir()
            (project / ".env.example").write_text(KIT_ENV)
            (project / ".gitignore").write_text(KIT_GITIGNORE)
        elif cmd[:3] == ["php", "artisan", "key:generate"]:
            env_file = installer_service.Path(cwd) / ".env"
            env_file.write_text(env_file.read_text().replace("APP_KEY=\n", "APP_KEY=base64:test\n"))

    monkeypatch.setattr(installer_service, "_run", fake_run)
    monkeypatch.setattr(installer_service, "_build_env", lambda: {})
    return TestClient(app)


def _get_inertia(client: TestClient, path: str, **params) -> zipfile.ZipFile:
    response = client.get(path, params={"app_name": "demo", "db": "sqlite", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


def _text(archive: zipfile.ZipFile, name: str) -> str:
    root = archive.namelist()[0].split("/")[0]  # the slugified app name
    return archive.read(f"{root}/{name}").decode()


def _dotenv(text: str) -> dict[str, str]:
    lines = text.splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))


# ── dotenv_quote ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("demo", '"demo"'),
        ("Minha Região", '"Minha Região"'),
        ("", '""'),
        ('say "hi"', '"say \\"hi\\""'),
        ("C:\\temp", '"C:\\\\temp"'),
        ("$HOME ${APP_ENV}", '"\\$HOME \\${APP_ENV}"'),
        # The backslash is escaped first, not twice: `\"` stays one backslash and one quote.
        ('\\"', '"\\\\\\""'),
        # A line break would start another line in the file (and in `_merge_env`).
        ("one\ntwo\r\nthree", '"one two  three"'),
        ("one\u2028two", '"one two"'),
    ],
)
def test_dotenv_quote(value, quoted):
    assert dotenv_quote(value) == quoted


def test_dotenv_quote_is_a_template_filter():
    template = installer_service._jinja_env.from_string("{{ name | dotenv_quote }}")

    assert template.render(name="Minha Região") == '"Minha Região"'


# ── _merge_env ───────────────────────────────────────────────────────────────


def test_merge_replaces_the_first_matching_line_in_place():
    assert _merge_env("A=1\nB=2\nC=3\n", "B=two\n") == "A=1\nB=two\nC=3\n"


def test_merge_replaces_a_commented_out_line():
    merged = _merge_env("DB_CONNECTION=sqlite\n# DB_HOST=127.0.0.1\n# DB_PORT=3306\n", "DB_CONNECTION=pgsql\nDB_HOST=db\n")

    assert merged == "DB_CONNECTION=pgsql\nDB_HOST=db\n# DB_PORT=3306\n"


def test_merge_uses_the_first_match_and_drops_the_later_duplicates():
    base = "# DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_HOST=other\n# DB_HOST=again\nZ=1\n"

    assert _merge_env(base, "DB_HOST=db\n") == "DB_HOST=db\nDB_PORT=3306\nZ=1\n"


def test_merge_appends_keys_the_base_lacks():
    assert _merge_env("A=1\n", "A=2\nB=3\nC=4\n") == "A=2\nB=3\nC=4\n"


def test_merge_keeps_every_other_line_and_comment():
    base = "# Mail\n\nMAIL_FROM_NAME=\"${APP_NAME}\"\n   # indented comment\nexport A=1\n"

    assert _merge_env(base, "A=2\n") == "# Mail\n\nMAIL_FROM_NAME=\"${APP_NAME}\"\n   # indented comment\nA=2\n"


def test_merge_does_not_match_a_longer_key_or_a_prose_comment():
    base = "DB_HOST_READ=x\n# Set DB_HOST=... to change it\nXDB_HOST=y\n"

    assert _merge_env(base, "DB_HOST=db\n") == base + "DB_HOST=db\n"


def test_merge_ignores_comments_and_blank_lines_in_the_overrides():
    assert _merge_env("A=1\n", "# A=2\n\nB=3\n") == "A=1\nB=3\n"


def test_merge_keeps_the_value_after_the_first_equals_sign():
    assert _merge_env("APP_KEY=\n", "APP_KEY=base64:a=b==\n") == "APP_KEY=base64:a=b==\n"


@pytest.mark.parametrize(
    ("base", "merged"),
    [
        ("", "A=1\n"),
        ("X=0", "X=0\nA=1\n"),
        ("X=0\n", "X=0\nA=1\n"),
        ("X=0\r\nY=1\r\n", "X=0\nY=1\nA=1\n"),
        ("X=0\n\n", "X=0\n\nA=1\n"),
    ],
)
def test_merge_ends_with_a_single_newline(base, merged):
    assert _merge_env(base, "A=1\n") == merged


def test_merge_is_idempotent():
    overrides = "APP_NAME=\"x y\"\nDB_HOST=db\nNEW=1\n"
    once = _merge_env(KIT_ENV, overrides)

    assert _merge_env(once, overrides) == once


# ── _gitignore_env_docker ────────────────────────────────────────────────────


def test_gitignore_gets_the_entry_appended(tmp_path):
    (tmp_path / ".gitignore").write_text(KIT_GITIGNORE)

    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text() == KIT_GITIGNORE + ".env.docker\n"


def test_gitignore_entry_is_added_once(tmp_path):
    (tmp_path / ".gitignore").write_text(KIT_GITIGNORE)

    _gitignore_env_docker(tmp_path)
    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text().splitlines().count(".env.docker") == 1


@pytest.mark.parametrize("existing", [".env.docker", "/.env.docker", "  .env.docker  "])
def test_gitignore_is_left_alone_when_the_file_is_listed(tmp_path, existing):
    content = f"/vendor\n{existing}\n.env\n"
    (tmp_path / ".gitignore").write_text(content)

    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text() == content


def test_gitignore_entry_goes_on_its_own_line_when_the_file_lacks_a_final_newline(tmp_path):
    (tmp_path / ".gitignore").write_text("/vendor")

    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text() == "/vendor\n.env.docker\n"


def test_gitignore_is_created_when_missing(tmp_path):
    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text() == ".env.docker\n"


def test_gitignore_does_not_take_a_similar_name_for_the_entry(tmp_path):
    (tmp_path / ".gitignore").write_text(".env.docker.bak\n# .env.docker\n")

    _gitignore_env_docker(tmp_path)

    assert (tmp_path / ".gitignore").read_text().endswith("\n.env.docker\n")


# ── The Inertia flows ────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", INERTIA_PATHS)
@pytest.mark.parametrize("name", [".env", ".env.docker"])
def test_the_kit_settings_survive_with_the_docker_values_in_place(client, path, name):
    text = _text(_get_inertia(client, path, db="postgres"), name)
    dotenv = _dotenv(text)

    # Replaced in place, including the commented-out ones.
    assert "DB_CONNECTION=pgsql\nDB_HOST=db\nDB_PORT=5432\nDB_DATABASE=laravel\nDB_USERNAME=laravel\nDB_PASSWORD=secret\n" in text
    assert "DB_CONNECTION=sqlite" not in text
    assert "127.0.0.1" not in text.split("REDIS_CLIENT")[0]
    assert dotenv["APP_KEY"] == "base64:test"
    assert dotenv["APP_URL"] == "http://localhost:8080"
    # The kit's own settings, which the old template dropped.
    for line in (
        "BCRYPT_ROUNDS=12",
        "LOG_STACK=single",
        "SESSION_DRIVER=database",
        "SESSION_LIFETIME=120",
        "BROADCAST_CONNECTION=log",
        "CACHE_STORE=database",
        "MAIL_MAILER=log",
        'MAIL_FROM_NAME="${APP_NAME}"',
        'VITE_APP_NAME="${APP_NAME}"',
        "APP_FAKER_LOCALE=en_US",
        "# CACHE_PREFIX=",
    ):
        assert line in text.splitlines(), line


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_every_kit_line_is_kept_or_replaced_in_place(client, path):
    lines = _text(_get_inertia(client, path, db="mysql"), ".env").splitlines()
    kit_lines = KIT_ENV.splitlines()

    assert len(lines) == len(kit_lines)
    # Only the settings Docker changes differ, and only by value.
    changed = {kit.split("=")[0].removeprefix("# ") for kit, line in zip(kit_lines, lines) if kit != line}
    expected = {
        "APP_NAME", "APP_KEY", "APP_URL", "DB_CONNECTION", "DB_HOST", "DB_PORT", "DB_DATABASE",
        "DB_USERNAME", "DB_PASSWORD",
    }
    # /v2 defaults to the kit's own `database` queue; /v1 has no queue option and uses `sync`.
    assert changed == expected | ({"QUEUE_CONNECTION"} if path.startswith("/v1") else set())


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_sqlite_points_at_the_container_path(client, path):
    text = _text(_get_inertia(client, path), ".env")

    assert "DB_CONNECTION=sqlite\n" in text
    assert "DB_DATABASE=/var/www/html/database/database.sqlite\n" in text
    assert text.count("DB_DATABASE=") == 1


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_env_and_env_docker_are_the_same_file(client, path):
    archive = _get_inertia(client, path)

    assert _text(archive, ".env") == _text(archive, ".env.docker")


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_each_key_is_set_once(client, path):
    keys = [line.split("=")[0] for line in _text(_get_inertia(client, path), ".env").splitlines() if "=" in line and not line.startswith("#")]

    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_an_app_name_with_a_space_is_quoted(client, path):
    text = _text(_get_inertia(client, path, app_name="Minha Região"), ".env")

    assert 'APP_NAME="Minha Região"\n' in text
    assert [line for line in text.splitlines() if line.startswith("APP_NAME=")] == ['APP_NAME="Minha Região"']
    # `VITE_APP_NAME` is the kit's and refers to it.
    assert 'VITE_APP_NAME="${APP_NAME}"\n' in text


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_special_characters_in_the_app_name_are_escaped(client, path):
    text = _text(_get_inertia(client, path, app_name='My "App" $HOME\nAPP_ENV=production'), ".env")

    assert 'APP_NAME="My \\"App\\" \\$HOME APP_ENV=production"\n' in text
    assert text.splitlines().count("APP_ENV=local") == 1
    assert "APP_ENV=production\n" not in text


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_env_docker_is_git_ignored_once(client, path):
    gitignore = _text(_get_inertia(client, path), ".gitignore")

    assert gitignore == KIT_GITIGNORE + ".env.docker\n"
    assert gitignore.splitlines().count(".env.docker") == 1


@pytest.mark.parametrize("path", INERTIA_PATHS)
def test_there_are_no_legacy_keys(client, path):
    for name in (".env", ".env.docker"):
        text = _text(_get_inertia(client, path), name)

        assert "CACHE_DRIVER" not in text
        assert "BROADCAST_DRIVER" not in text
        assert "legacy" not in text.lower()


def test_v1_inertia_keeps_the_sync_queue(client):
    assert "QUEUE_CONNECTION=sync\n" in _text(_get_inertia(client, "/v1/new-inertia"), ".env")


def test_v2_queue_follows_the_option(client):
    for queue in ("database", "sync"):
        text = _text(_get_inertia(client, "/v2/new-inertia", queue=queue), ".env")

        assert f"QUEUE_CONNECTION={queue}\n" in text


def test_v2_queue_and_redis_are_merged_into_the_kit_settings(client):
    text = _text(_get_inertia(client, "/v2/new-inertia", queue="redis"), ".env")

    assert "QUEUE_CONNECTION=redis\n" in text
    assert "REDIS_CLIENT=predis\nREDIS_HOST=redis\nREDIS_PASSWORD=null\nREDIS_PORT=6379\n" in text
    assert "phpredis" not in text


def test_v2_locale_replaces_the_kits_locale_in_place(client):
    text = _text(_get_inertia(client, "/v2/new-inertia", locale="pt_BR"), ".env")

    assert "APP_LOCALE=pt_BR\nAPP_FALLBACK_LOCALE=pt_BR\nAPP_FAKER_LOCALE=en_US\n" in text


def test_v1_inertia_keeps_the_kits_locale(client):
    text = _text(_get_inertia(client, "/v1/new-inertia"), ".env")

    assert "APP_LOCALE=en\nAPP_FALLBACK_LOCALE=en\n" in text


# ── /v1/release ──────────────────────────────────────────────────────────────

VERSION = "v12.0.0"
RELEASE_ROOT = "laravel-framework-12.0.0/"


@pytest.fixture
def release_client(monkeypatch):
    versions = Enum("LaravelVersion", {"latest": VERSION})

    async def fake_download(version: str) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr(RELEASE_ROOT + "composer.json", "{}")
        return buffer.getvalue()

    monkeypatch.setattr(github_service, "get_version_enum", lambda: versions)
    monkeypatch.setattr(github_service, "download_release_zip", fake_download)
    return TestClient(app)


def _get_release(client: TestClient, **params) -> zipfile.ZipFile:
    response = client.get("/v1/release", params={"version": VERSION, "db": "mysql", **params})
    assert response.status_code == 200, response.text
    return zipfile.ZipFile(io.BytesIO(response.content))


@pytest.mark.parametrize("name", [".env", ".env.docker"])
def test_release_still_writes_the_legacy_keys(release_client, name):
    text = _get_release(release_client).read(RELEASE_ROOT + name).decode()

    assert "BROADCAST_DRIVER=log\n" in text
    assert "CACHE_DRIVER=file\n" in text
    assert "SESSION_DRIVER=file\n" in text


@pytest.mark.parametrize("db", ["mysql", "postgres", "sqlite"])
def test_release_env_has_no_blank_lines_and_ends_with_a_newline(release_client, db):
    text = _get_release(release_client, db=db).read(RELEASE_ROOT + ".env").decode()

    assert text.endswith("\n")
    assert "" not in text.splitlines()
    assert "\n\n" not in text


def test_release_env_keeps_the_settings_it_always_had(release_client):
    dotenv = _dotenv(_get_release(release_client, app_name="demo").read(RELEASE_ROOT + ".env").decode())

    assert dotenv["LOG_CHANNEL"] == "stack"
    assert dotenv["FILESYSTEM_DISK"] == "local"
    assert dotenv["SESSION_LIFETIME"] == "120"
    assert dotenv["QUEUE_CONNECTION"] == "sync"
    assert dotenv["DB_CONNECTION"] == "mysql"
    assert dotenv["DB_HOST"] == "db"
    assert dotenv["APP_KEY"].startswith("base64:")


def test_release_quotes_the_app_name(release_client):
    text = _get_release(release_client, app_name="Minha Região").read(RELEASE_ROOT + ".env").decode()

    assert 'APP_NAME="Minha Região"\n' in text


def test_the_template_has_no_legacy_keys_without_the_flag():
    template = installer_service._jinja_env.get_template(".env.docker.j2")
    context = {"app_name": "demo", "app_port": "8000", "db": "sqlite"}

    assert "CACHE_DRIVER" not in template.render(context)
    assert "CACHE_DRIVER=file\n" in template.render({**context, "legacy_env_keys": True})
