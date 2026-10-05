"""
The installer's own ``README.md`` against the API it documents.

``GET /v2/new-inertia`` has to be documented for every parameter Swagger UI (``/docs``) shows,
which is what the app's OpenAPI document lists, with the same defaults and values. The archive
layout has to list the files the endpoint generates, the project-structure list every template
and router module, and the production fixes the README claims have to exist in the generator.
"""

import re
from pathlib import Path

import pytest

from main import app
from services import installer_service
from services.installer_service import SCAFFOLD_DIR, slugify_app_name

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()

V2_HEADING = "### `GET /v2/new-inertia`"


def _cells(row: str) -> list[str]:
    """The cells of a table row; only ``\\|`` keeps a pipe inside a cell."""
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def _v2_section() -> str:
    body = README.split(f"\n{V2_HEADING}")[1]
    return body[: re.search(r"^## ", body, re.MULTILINE).start()]


def _before_v2() -> str:
    return README.split(f"\n{V2_HEADING}")[0]


def _rows(text: str, first_header: str = "Parameter") -> dict[str, list[str]]:
    """Body rows of the tables whose first header cell is ``first_header``, keyed by the backticked name."""
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("|"):
            cells = _cells(line)
            if cells[0] == first_header:
                in_table = True
            elif in_table and (match := re.fullmatch(r"`(\w+)`", cells[0])):
                rows[match.group(1)] = cells
        else:
            in_table = False
    return rows


def _openapi_parameters(path: str) -> dict[str, dict]:
    spec = app.openapi()
    return {param["name"]: param for param in spec["paths"][path]["get"]["parameters"]}


V1 = _openapi_parameters("/v1/new-inertia")
V2 = _openapi_parameters("/v2/new-inertia")
NEW = [name for name in V2 if name not in V1]


def test_swagger_still_shows_the_parameters_this_file_expects():
    # A guard for the other tests: if these change, the README needs a look.
    assert V1.keys() <= V2.keys()
    assert {"locale", "queue", "horizon", "telescope", "proxy_network", "max_upload_mb", "ci_provider"} == set(NEW)


def test_every_new_parameter_is_in_the_v2_section_and_nothing_else_is():
    documented = _rows(_v2_section())

    assert set(documented) == set(NEW)


@pytest.mark.parametrize("name", V1)
def test_the_parameters_shared_with_v1_are_documented_and_unchanged(name):
    # The v2 section says they are accepted "with the same name, values and default".
    assert name in _rows(_before_v2()), f"/v1/new-inertia tables do not document {name}"
    shared = ("default", "enum", "type", "minimum", "maximum", "pattern")
    assert {key: V1[name]["schema"].get(key) for key in shared} == {key: V2[name]["schema"].get(key) for key in shared}


def _schema_options(schema: dict) -> dict:
    """The schema of a parameter, looking through ``Optional[...]`` (``anyOf`` with ``null``)."""
    for option in schema.get("anyOf", []):
        if option.get("type") != "null":
            return {**schema, **option}
    return schema


@pytest.mark.parametrize("name", NEW)
def test_the_documented_type_and_default_match_swagger(name):
    _, type_cell, default_cell, description = _rows(_v2_section())[name]
    schema = _schema_options(V2[name]["schema"])
    default = schema.get("default")

    expected_default = "—" if default is None else f"`{str(default).lower() if isinstance(default, bool) else default}`"
    assert default_cell == expected_default
    assert V2[name].get("required") is not True and description

    if "enum" in schema:
        assert type_cell == " | ".join(f"`{value}`" for value in schema["enum"])
    else:
        assert type_cell.startswith(f"`{schema['type']}`")
    if "minimum" in schema:
        assert f"({schema['minimum']}–{schema['maximum']})" in type_cell


def test_the_documented_error_responses_are_the_ones_swagger_lists():
    codes = [code for code in app.openapi()["paths"]["/v2/new-inertia"]["get"]["responses"] if code != "200"]

    assert codes
    for code in codes:
        assert f"`{code}`" in _v2_section()


# ── Archive layout and quick start ────────────────────────────────────────────


def _archive_layout() -> str:
    return re.search(r"\*\*Archive contents:\*\*\s+```\n(.*?)```", _v2_section(), re.DOTALL).group(1)


def test_the_archive_layout_lists_every_generated_file():
    generated = [dest for _, dest, *_ in installer_service.INERTIA_SERVER_TEMPLATES_V2]
    generated += [dest for _, dest in installer_service.CI_TEMPLATES.values()]
    generated += [".env", ".env.docker"]
    layout = _archive_layout()

    for dest in generated:
        assert Path(dest).name in layout, dest
    # Nested paths sit under their folders, and the optional files say when they exist.
    assert "docker/" in layout and "php/app.ini" in layout
    for provider, (_, dest) in installer_service.CI_TEMPLATES.items():
        assert f"{dest}" in layout and f"ci_provider={provider}" in layout


def test_the_slug_rule_and_the_quick_start_match_the_generator():
    section = _v2_section()

    assert slugify_app_name("Café & Co") == "caf-co"
    assert "`Café & Co` becomes `caf-co`" in section
    assert slugify_app_name("my-app") == "my-app"
    assert f"unzip laravel-{slugify_app_name('my-app')}-inertia-react-docker.zip\ncd my-app\n" in section
    assert "laravel-<app_slug>-inertia-<kit>-docker.zip" in _archive_layout()


# ── Project structure ─────────────────────────────────────────────────────────


def _structure() -> str:
    return re.search(r"## Project structure\s+```\n(.*?)```", README, re.DOTALL).group(1)


def test_the_project_structure_lists_every_router_module_and_template():
    structure = _structure()

    for module in sorted((ROOT / "api" / "dto" / "router").glob("*.py")):
        if module.name != "__init__.py":
            assert module.name in structure, module.name
    assert re.search(r"v2\.py\s+Route definitions \(/v2/new-inertia\)", structure)
    for template in sorted(SCAFFOLD_DIR.iterdir()):
        assert template.name in structure, template.name


def test_the_requirements_and_intro_cover_v2():
    requirements = README.split("## Requirements")[1].split("```")[0]

    assert requirements.count("`/v1/new-inertia` and `/v2/new-inertia`") == 2
    assert "Both modes" not in README and "both\nendpoints" not in README


# ── The production fixes it claims ────────────────────────────────────────────

CORPUS = "\n".join(
    [(ROOT / "api" / "services" / "installer_service.py").read_text()]
    + [path.read_text() for path in sorted(SCAFFOLD_DIR.iterdir())]
)

# (what the README says, what the generator or templates contain for it)
CLAIMS = [
    ("trustProxies", "trustProxies"),
    ("URL::forceScheme('https')", "URL::forceScheme('https')"),
    ("AddLinkHeadersForPreloadedAssets", "AddLinkHeadersForPreloadedAssets"),
    ("@import", "_REMOTE_FONT_IMPORT_RE"),
    ("`server.host` and `hmr.host`", "hmr: {"),
    ("`public/hot`", "public/hot"),
    ("client_max_body_size", "client_max_body_size"),
    ("upload_max_filesize", "upload_max_filesize"),
    ("post_max_size", "post_max_size"),
    ("FastCGI header buffers are 16k", "fastcgi_buffer_size 16k"),
    ("`memory_limit` is 256M", "memory_limit = 256M"),
    ("`public/storage` link", "storage:link"),
    ("`WORKER_STOP_GRACE_PERIOD` (default `90s`)", "WORKER_STOP_GRACE_PERIOD:-90s"),
    ("`APP_DEBUG=false`", 'APP_DEBUG: "false"'),
    ("registered only when the package is installed", "class_exists(TelescopeApplicationServiceProvider::class)"),
]


@pytest.mark.parametrize(("readme_text", "generated_text"), CLAIMS)
def test_each_production_fix_in_the_readme_exists_in_the_generator(readme_text, generated_text):
    assert readme_text in _v2_section(), readme_text
    assert generated_text in CORPUS, generated_text
