"""
``ci_provider`` of ``GET /v2/new-inertia``: an image build-and-push pipeline.

``none`` (the default) adds no file. ``github`` adds exactly
``.github/workflows/docker-publish.yml`` and ``gitlab`` exactly ``.gitlab-ci.yml``. Both build
the production ``Dockerfile`` and push it to the provider's registry, so the staging/production
stacks can deploy it with ``APP_IMAGE`` / ``IMAGE_TAG``, ``pull`` and ``up -d --no-build``.

The pipelines cannot run here, so the tests check what can be checked offline: the YAML parses,
the GitHub workflow has the documented triggers, steps and tag rules, and the GitLab jobs are
evaluated against simulated pipelines (their ``rules:if`` conditions are run, and the tags a job
builds must be the tags it pushes). The README and the header comments are checked for the
tagging scheme and the ``APP_IMAGE`` / ``IMAGE_TAG`` deploy command. ``_run`` is replaced by a
recorder (no Composer, PHP or npm needed). The test that calls ``docker compose config`` is
skipped when the Compose plugin is not installed.
"""

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from services import installer_service

PROVIDERS = ["github", "gitlab"]
FILES = {
    "github": ".github/workflows/docker-publish.yml",
    "gitlab": ".gitlab-ci.yml",
}
STACKS = ["docker-compose.stage.yml", "docker-compose.prod.yml"]

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


def _files(archive: zipfile.ZipFile) -> set[str]:
    root = archive.namelist()[0].split("/")[0]
    return {name.removeprefix(f"{root}/") for name in archive.namelist() if not name.endswith("/")}


def _pipeline(client: TestClient, provider: str) -> str:
    return _text(_get_project(client, ci_provider=provider), FILES[provider])


def _workflow(client: TestClient) -> dict:
    workflow = yaml.safe_load(_pipeline(client, "github"))
    # YAML 1.1 reads the bare key `on` as the boolean True.
    workflow["on"] = workflow.pop(True)
    return workflow


def _step(workflow: dict, name: str) -> dict:
    steps = workflow["jobs"]["build-and-push"]["steps"]
    return next(step for step in steps if step["name"] == name)


def _lines(block: str) -> list[str]:
    return [line.strip() for line in block.splitlines() if line.strip()]


# ── Validation and the option itself ─────────────────────────────────────────


@pytest.mark.parametrize("value", ["bitbucket", "GitHub", "GITLAB", "github ", "git hub", "true", ""])
def test_unknown_providers_are_rejected(client, value):
    response = client.get("/v2/new-inertia", params={"app_name": "demo", "ci_provider": value})

    assert response.status_code == 422


def test_the_parameter_is_optional_defaults_to_none_and_is_documented(client):
    spec = client.get("/openapi.json").json()
    parameters = spec["paths"]["/v2/new-inertia"]["get"]["parameters"]
    ci_provider = next(p for p in parameters if p["name"] == "ci_provider")

    assert ci_provider["required"] is False
    assert ci_provider["schema"]["default"] == "none"
    assert ci_provider["schema"]["enum"] == ["none", "github", "gitlab"]
    for text in (".github/workflows/docker-publish.yml", ".gitlab-ci.yml", "latest", "stable"):
        assert text in ci_provider["description"]


def test_the_templates_map_names_one_file_per_provider():
    assert installer_service.CI_TEMPLATES == {
        "github": ("github-docker-publish.yml.j2", ".github/workflows/docker-publish.yml"),
        "gitlab": ("gitlab-ci.yml.j2", ".gitlab-ci.yml"),
    }


# ── Files in the archive ─────────────────────────────────────────────────────


def test_none_is_the_default_and_adds_no_file(client):
    default = _files(_get_project(client))

    assert _files(_get_project(client, ci_provider="none")) == default
    assert not {name for name in default if name.startswith(".github") or name == ".gitlab-ci.yml"}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_provider_adds_exactly_one_file(client, provider):
    without = _files(_get_project(client))
    with_ci = _files(_get_project(client, ci_provider=provider))

    assert with_ci - without == {FILES[provider]}
    assert without - with_ci == set()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_pipeline_is_valid_yaml_with_nothing_left_unrendered(client, provider):
    text = _pipeline(client, provider)

    assert isinstance(yaml.safe_load(text), dict)
    assert "{%" not in text and "{#" not in text
    assert not text.startswith("\n")
    assert text.endswith("\n") and not text.endswith("\n\n")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_other_files_do_not_change_with_the_provider(client, provider):
    shared = {"Dockerfile", "docker-compose.yml", "docker-compose.stage.yml", "docker-compose.prod.yml", ".dockerignore"}
    base = _get_project(client)
    with_ci = _get_project(client, ci_provider=provider)

    for name in shared:
        assert _text(with_ci, name) == _text(base, name), name


# ── GitHub Actions ───────────────────────────────────────────────────────────


def test_github_triggers(client):
    on = _workflow(client)["on"]

    assert on["push"]["branches"] == ["main", "master", "develop"]
    assert on["push"]["tags"] == ["v*.*.*"]
    assert on["pull_request"]["branches"] == ["main", "master", "develop"]


def test_github_job_can_only_write_packages(client):
    job = _workflow(client)["jobs"]["build-and-push"]

    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read", "packages": "write"}


def test_github_pull_requests_build_but_do_not_log_in_or_push(client):
    workflow = _workflow(client)

    assert _step(workflow, "Log in to GitHub Container Registry")["if"] == "github.event_name != 'pull_request'"
    assert _step(workflow, "Build and push")["with"]["push"] == "${{ github.event_name != 'pull_request' }}"


def test_github_logs_in_to_ghcr_with_the_workflow_token(client):
    login = _step(_workflow(client), "Log in to GitHub Container Registry")

    assert login["uses"].startswith("docker/login-action@")
    assert login["with"] == {
        "registry": "ghcr.io",
        "username": "${{ github.actor }}",
        "password": "${{ secrets.GITHUB_TOKEN }}",
    }


def test_github_tag_rules(client):
    metadata = _step(_workflow(client), "Extract metadata")

    assert metadata["uses"].startswith("docker/metadata-action@")
    assert metadata["with"]["images"] == "ghcr.io/${{ github.repository }}"
    assert _lines(metadata["with"]["tags"]) == [
        "type=sha",
        "type=ref,event=branch",
        "type=raw,value=latest,enable={{is_default_branch}}",
        "type=raw,value=stable,enable=${{ startsWith(github.ref, 'refs/tags/v') }}",
        "type=semver,pattern={{version}}",
        "type=semver,pattern={{major}}.{{minor}}",
        "type=semver,pattern={{major}}",
    ]


def test_github_latest_follows_only_the_default_branch(client):
    # metadata-action adds `latest` to every version tag unless told not to.
    assert _lines(_step(_workflow(client), "Extract metadata")["with"]["flavor"]) == ["latest=false"]


def test_github_builds_the_dockerfile_and_pushes_the_metadata_tags_with_a_gha_cache(client):
    workflow = _workflow(client)
    metadata_id = _step(workflow, "Extract metadata")["id"]
    build = _step(workflow, "Build and push")

    assert build["uses"].startswith("docker/build-push-action@")
    assert build["with"]["context"] == "."
    assert build["with"]["file"] == "./Dockerfile"
    assert build["with"]["tags"] == f"${{{{ steps.{metadata_id}.outputs.tags }}}}"
    assert build["with"]["labels"] == f"${{{{ steps.{metadata_id}.outputs.labels }}}}"
    assert build["with"]["cache-from"] == "type=gha"
    assert build["with"]["cache-to"] == "type=gha,mode=max"


def test_github_runs_checkout_and_buildx_before_building(client):
    names = [step["name"] for step in _workflow(client)["jobs"]["build-and-push"]["steps"]]

    assert names == [
        "Checkout",
        "Set up Docker Buildx",
        "Log in to GitHub Container Registry",
        "Extract metadata",
        "Build and push",
    ]


def test_github_expressions_are_not_swallowed_by_jinja(client):
    text = _pipeline(client, "github")

    assert "${{ github.repository }}" in text
    assert "{{is_default_branch}}" in text


# ── GitLab CI ────────────────────────────────────────────────────────────────

# What GitLab sets in each kind of pipeline (the variables the jobs use).
PIPELINES = {
    "default branch": {
        "CI_COMMIT_BRANCH": "main",
        "CI_COMMIT_REF_SLUG": "main",
    },
    "other branch": {
        "CI_COMMIT_BRANCH": "feature/Add-Login",
        "CI_COMMIT_REF_SLUG": "feature-add-login",
    },
    "tag": {
        "CI_COMMIT_TAG": "v1.2.3",
        "CI_COMMIT_REF_SLUG": "v1-2-3",
    },
}
EXPECTED_TAGS = {
    "default branch": {"abc12345", "latest"},
    "other branch": {"abc12345", "feature-add-login"},
    "tag": {"abc12345", "v1.2.3", "stable"},
}
GITLAB_ENV = {
    "CI_DEFAULT_BRANCH": "main",
    "CI_COMMIT_SHORT_SHA": "abc12345",
    "CI_REGISTRY": "registry.example.com",
    "CI_REGISTRY_IMAGE": "registry.example.com/group/project",
    "CI_REGISTRY_USER": "gitlab-ci-token",
    "CI_REGISTRY_PASSWORD": "token",
}


def _gitlab(client: TestClient) -> dict:
    return yaml.safe_load(_pipeline(client, "gitlab"))


def _job(ci: dict, name: str) -> dict:
    """The job with its ``extends`` template merged in (the template only sets plain keys)."""
    job = ci[name]
    return {**ci[job["extends"]], **job} if "extends" in job else job


def _jobs(ci: dict) -> list[str]:
    return [name for name in ci if not name.startswith(".") and isinstance(ci[name], dict) and "script" in ci[name]]


def _matches(rule: dict, env: dict) -> bool:
    """Evaluate a ``rules:if`` condition (``$VAR``, ``==``, ``!=``, ``&&``, ``||``) against ``env``."""
    expression = re.sub(r"\$(\w+)", r'env.get("\1")', rule["if"]).replace("&&", " and ").replace("||", " or ")
    return bool(eval(expression, {"__builtins__": {}}, {"env": env}))  # noqa: S307 - our own template


def _matching_jobs(ci: dict, env: dict) -> list[str]:
    return [name for name in _jobs(ci) if any(_matches(rule, env) for rule in ci[name]["rules"])]


def _expand(text: str, env: dict) -> str:
    return re.sub(r"\$(\w+)", lambda match: env[match.group(1)], text)


def _docker_args(job: dict, env: dict, command: str) -> list[list[str]]:
    lines = [_expand(line, env) for line in job["script"] if line.startswith(f"docker {command} ")]
    return [shlex.split(line) for line in lines]


def test_gitlab_runs_docker_in_docker_with_the_27_images(client):
    job = _job(_gitlab(client), "build:default-branch")

    assert job["stage"] == "build"
    assert job["image"] == "docker:27-cli"
    assert job["services"] == ["docker:27-dind"]


def test_gitlab_reaches_the_dind_daemon_over_tls(client):
    variables = _job(_gitlab(client), "build:default-branch")["variables"]

    assert variables["DOCKER_HOST"] == "tcp://docker:2376"
    assert variables["DOCKER_TLS_CERTDIR"] == "/certs"
    assert variables["DOCKER_TLS_VERIFY"] == "1"
    assert variables["DOCKER_CERT_PATH"] == "/certs/client"


def test_gitlab_logs_in_to_the_project_registry_with_the_password_on_stdin(client):
    ci = _gitlab(client)

    for name in _jobs(ci):
        login = _job(ci, name)["before_script"]
        assert login == [
            'echo "$CI_REGISTRY_PASSWORD" | docker login -u "$CI_REGISTRY_USER" --password-stdin "$CI_REGISTRY"'
        ], name
    assert _job(ci, "build:tag")["variables"]["IMAGE_NAME"] == "$CI_REGISTRY_IMAGE"
    assert "--password " not in _pipeline(client, "gitlab").replace("--password-stdin", "")


def test_gitlab_has_one_job_per_kind_of_pipeline(client):
    assert _jobs(_gitlab(client)) == ["build:default-branch", "build:branch", "build:tag"]


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_gitlab_exactly_one_job_runs_in_each_kind_of_pipeline(client, pipeline):
    expected = {
        "default branch": ["build:default-branch"],
        "other branch": ["build:branch"],
        "tag": ["build:tag"],
    }[pipeline]

    assert _matching_jobs(_gitlab(client), {**GITLAB_ENV, **PIPELINES[pipeline]}) == expected


@pytest.mark.parametrize("pipeline", PIPELINES)
def test_gitlab_builds_and_pushes_the_documented_tags(client, pipeline):
    ci = _gitlab(client)
    env = {**GITLAB_ENV, **PIPELINES[pipeline]}
    (name,) = _matching_jobs(ci, env)
    job = _job(ci, name)
    env = {**env, "IMAGE_NAME": env["CI_REGISTRY_IMAGE"]}
    image = env["IMAGE_NAME"]

    (build,) = _docker_args(job, env, "build")
    built = {build[i + 1] for i, arg in enumerate(build) if arg == "-t"}
    pushed = {args[2] for args in _docker_args(job, env, "push")}

    assert build[:4] == ["docker", "build", "-f", "Dockerfile"] and build[-1] == "."
    assert built == {f"{image}:{tag}" for tag in EXPECTED_TAGS[pipeline]}
    assert pushed == built
    assert len(_docker_args(job, env, "push")) == len(built)


def test_gitlab_tags_are_valid_docker_tags_for_awkward_branch_names(client):
    # The branch slug, not the branch name: `feature/Add-Login` has a slash and a capital.
    ci = _gitlab(client)
    env = {**GITLAB_ENV, **PIPELINES["other branch"], "IMAGE_NAME": GITLAB_ENV["CI_REGISTRY_IMAGE"]}
    (build,) = _docker_args(_job(ci, "build:branch"), env, "build")

    for arg in build:
        if arg.startswith(GITLAB_ENV["CI_REGISTRY_IMAGE"] + ":"):
            assert re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", arg.split(":", 1)[1]), arg


# ── Header comments ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "image"),
    [("github", "ghcr.io/<owner>/<repo>"), ("gitlab", "<registry>/<group>/<project>")],
)
def test_the_header_shows_the_deploy_command_for_the_pushed_image(client, provider, image):
    text = _pipeline(client, provider)
    header = text.split("\nname:" if provider == "github" else "\nstages:")[0]

    assert f"APP_IMAGE={image} IMAGE_TAG=<tag> docker compose -f docker-compose.prod.yml pull" in header
    assert f"APP_IMAGE={image} IMAGE_TAG=<tag> docker compose -f docker-compose.prod.yml up -d --no-build" in header
    assert all(line.startswith("#") for line in header.splitlines())


# ── README ───────────────────────────────────────────────────────────────────


def _readme(client: TestClient, **params) -> str:
    return _text(_get_project(client, **params), "README.md")


def test_readme_without_a_provider_mentions_no_pipeline(client):
    readme = _readme(client)

    assert "docker-publish" not in readme
    assert ".gitlab-ci" not in readme
    assert "CI pipeline" not in readme
    assert _readme(client, ci_provider="none") == readme


@pytest.mark.parametrize("provider", PROVIDERS)
def test_readme_lists_the_file_in_the_archive_contents_table(client, provider):
    readme = _readme(client, ci_provider=provider)
    table = readme.split("## What's inside this archive")[1].split("\n## ")[0]
    rows = [line for line in table.splitlines() if f"`{FILES[provider]}`" in line]

    assert len(rows) == 1 and rows[0].startswith("| ") and rows[0].endswith(" |")
    assert "[CI pipeline](#ci-pipeline)" in rows[0]
    # The row stays inside the table, which ends at the blank line before the next heading.
    assert table.rstrip("\n").endswith("|")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_readme_shows_the_provider_in_the_configuration_table(client, provider):
    table = _readme(client, ci_provider=provider).split("## Project configuration")[1].split("\n## ")[0]
    name = {"github": "GitHub Actions", "gitlab": "GitLab CI"}[provider]

    assert table.count("| CI pipeline |") == 1
    assert f"| CI pipeline | {name} " in table


def test_readme_documents_the_github_tagging_scheme(client):
    section = _readme(client, ci_provider="github").split("## CI pipeline")[1].split("\n## ")[0]

    assert section.count("## ") == 0
    for text in (
        "ghcr.io/<owner>/<repo>",
        "`sha-<short sha>`",
        "the branch name",
        "`latest`",
        "`stable`",
        "`1.2.3`, `1.2` and `1`",
        "build only",
        "export APP_IMAGE=ghcr.io/<owner>/<repo> IMAGE_TAG=<tag>",
        "docker compose -f docker-compose.prod.yml pull",
        "docker compose -f docker-compose.prod.yml up -d --no-build",
    ):
        assert text in section, text


def test_readme_documents_the_gitlab_tagging_scheme(client):
    section = _readme(client, ci_provider="gitlab").split("## CI pipeline")[1].split("\n## ")[0]

    for text in (
        "docker:27-dind",
        "$CI_REGISTRY_IMAGE",
        "`<short sha>` and `latest`",
        "the branch slug",
        "the tag name (`v1.2.3`) and `stable`",
        "export APP_IMAGE=<registry>/<group>/<project> IMAGE_TAG=<tag>",
        "docker compose -f docker-compose.prod.yml pull",
        "docker compose -f docker-compose.prod.yml up -d --no-build",
    ):
        assert text in section, text


@pytest.mark.parametrize("provider", PROVIDERS)
def test_readme_has_one_ci_section_that_the_links_point_to(client, provider):
    readme = _readme(client, ci_provider=provider)

    assert readme.count("\n## CI pipeline\n") == 1
    assert "](#ci-pipeline)" in readme


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_readme_deploy_command_matches_the_pipeline_header(client, provider):
    archive = _get_project(client, ci_provider=provider)
    section = _text(archive, "README.md").split("## CI pipeline")[1].split("\n## ")[0]
    image = re.search(r"^export APP_IMAGE=(\S+) IMAGE_TAG=<tag>$", section, re.M).group(1)

    assert f"APP_IMAGE={image} IMAGE_TAG=<tag> docker compose -f docker-compose.prod.yml pull" in _text(
        archive, FILES[provider]
    )


# ── The /v1 flows are unchanged ──────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/v1/new-inertia", "/v1/release"])
def test_v1_has_no_ci_provider_parameter(client, path):
    parameters = client.get("/openapi.json").json()["paths"][path]["get"]["parameters"]

    assert "ci_provider" not in {p["name"] for p in parameters}


@pytest.mark.parametrize("provider", PROVIDERS)
def test_v1_output_has_no_pipeline_and_a_ci_provider_param_is_ignored(client, provider):
    response = client.get("/v1/new-inertia", params={"app_name": "demo", "ci_provider": provider})
    assert response.status_code == 200, response.text

    assert not {name for name in _files(zipfile.ZipFile(io.BytesIO(response.content))) if name in FILES.values()}


# ── Real `docker compose config` ─────────────────────────────────────────────


def _compose_config(tmp_path, text: str, env: dict[str, str]):
    """Run ``docker compose config`` on ``text`` with a clean shell and no env file."""
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "compose", "version"], capture_output=True
    ).returncode:
        pytest.skip("docker compose is not installed")
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(text)
    return subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-f", str(compose_file), "config", "--format", "json"],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **env},
    )


@pytest.mark.parametrize("stack", STACKS)
@pytest.mark.parametrize(
    ("provider", "image", "tag"),
    [
        ("github", "ghcr.io/acme/demo", "sha-abc1234"),
        ("github", "ghcr.io/acme/demo", "1.2.3"),
        ("gitlab", "registry.example.com/group/project", "abc12345"),
        ("gitlab", "registry.example.com/group/project", "v1.2.3"),
    ],
)
def test_config_deploys_the_pushed_image_without_building(client, tmp_path, stack, provider, image, tag):
    archive = _get_project(client, ci_provider=provider, queue="database")
    result = _compose_config(
        tmp_path, _text(archive, stack), {**REQUIRED, "APP_IMAGE": image, "IMAGE_TAG": tag}
    )

    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("app", "worker"):
        assert services[name]["image"] == f"{image}:{tag}", name
