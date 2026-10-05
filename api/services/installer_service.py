import asyncio
import base64
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

logger = logging.getLogger(__name__)

SCAFFOLD_DIR = Path(__file__).parent.parent / "scaffold"

# Maps each template file (relative to SCAFFOLD_DIR) to its destination
# path inside the zip (relative to the Laravel root folder).
TEMPLATES: list[tuple[str, str]] = [
    ("Dockerfile.j2", "Dockerfile"),
    ("docker-compose.yml.j2", "docker-compose.yml"),
    (".env.docker.j2", ".env.docker"),
    ("README.docker.md.j2", "README.md"),
]

# Templates injected into the server-generated Inertia project (v1: dev-only,
# php-cli + php artisan serve).
INERTIA_SERVER_TEMPLATES: list[tuple[str, str]] = [
    ("Dockerfile-inertia.j2", "Dockerfile"),
    ("docker-compose-inertia.yml.j2", "docker-compose.yml"),
    ("entrypoint.sh.j2", "entrypoint.sh"),
    ("README.inertia.md.j2", "README.md"),
]

# Templates injected into the server-generated Inertia project (v2: adds
# staging/production stacks running php-fpm + nginx, with dev vs. stage/prod
# entrypoints split out).
INERTIA_SERVER_TEMPLATES_V2: list[tuple[str, str]] = [
    ("Dockerfile-inertia-v2.j2", "Dockerfile"),
    ("docker-compose-inertia-v2.yml.j2", "docker-compose.yml"),
    ("docker-compose-inertia-stage.yml.j2", "docker-compose.stage.yml"),
    ("docker-compose-inertia-prod.yml.j2", "docker-compose.prod.yml"),
    ("nginx.conf.j2", "docker/nginx.conf"),
    ("dev.entrypoint.sh.j2", "docker/dev.entrypoint.sh"),
    ("prod.entrypoint.sh.j2", "docker/prod.entrypoint.sh"),
    ("dockerignore.j2", ".dockerignore"),
    ("README.inertia-v2.md.j2", "README.md"),
]

# Composer package for each Inertia starter kit.
_STARTER_KIT_PACKAGES: dict[str, str] = {
    "react": "laravel/react-starter-kit",
    "vue": "laravel/vue-starter-kit",
    "livewire": "laravel/livewire-starter-kit",
    "livewire-class-components": "laravel/livewire-starter-kit",
}

# Valid auth feature keys accepted by `php artisan install:features --answers`
AUTH_FEATURE_KEYS: set[str] = {
    "email-verification",
    "registration",
    "2fa",
    "passkeys",
    "password-confirmation",
}

_jinja_env = Environment(
    loader=FileSystemLoader(str(SCAFFOLD_DIR)),
    undefined=StrictUndefined,
    keep_trailing_newline=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def generate_app_key() -> str:
    """Generate a Laravel-compatible APP_KEY (base64:… of 32 random bytes)."""
    return "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()


def slugify_app_name(app_name: str) -> str:
    """
    Turn a user-supplied app name into a safe slug usable as a single
    filesystem path component (no ``..``, ``/``, or other separators).
    """
    slug = re.sub(r"[^a-z0-9-]+", "-", app_name.lower()).strip("-")
    return slug or "app"


def _starter_kit_ref(starter_kit: str, auth_provider: str, teams: bool) -> str:
    """
    Return the ``composer create-project`` package reference (name[:branch])
    for the requested starter kit, taking workos / teams variants into account.
    """
    package = _STARTER_KIT_PACKAGES[starter_kit]
    if starter_kit == "livewire-class-components":
        return f"{package}:dev-components"
    branch: str | None = {
        ("workos", True): "dev-workos-teams",
        ("workos", False): "dev-workos",
        ("laravel", True): "dev-teams",
    }.get((auth_provider, teams))
    return f"{package}:{branch}" if branch else package


_REMOTE_FONT_IMPORT_RE = re.compile(
    r"^\s*@import\s+url\(['\"]https?://fonts\.(?:bunny\.net|googleapis\.com)[^'\"]*['\"]\)\s*;\s*$",
    re.MULTILINE,
)


def _strip_remote_font_imports(project_dir: Path) -> None:
    """
    Remove ``@import url(...)`` lines pulling Google/Bunny fonts into the
    generated project's CSS.

    Recent ``laravel-vite-plugin`` versions self-host these fonts by
    fetching them from the network during ``npm run build`` (triggered by
    chisel's ``install:features`` apply step). The installer server has no
    route to those font CDNs, so the fetch hangs until it times out and
    fails the whole build. Stripping the import keeps the build local;
    the app simply falls back to the default Tailwind font stack.
    """
    css_dir = project_dir / "resources" / "css"
    if not css_dir.is_dir():
        return
    for css_file in css_dir.rglob("*.css"):
        original = css_file.read_text()
        stripped = _REMOTE_FONT_IMPORT_RE.sub("", original)
        if stripped != original:
            css_file.write_text(stripped)


_VITE_DEFINE_CONFIG_RE = re.compile(r"defineConfig\(\{")

_VITE_DEV_SERVER_CONFIG = """server: {
        host: '0.0.0.0',
        hmr: {
            host: 'localhost',
        },
    },
"""


def _configure_vite_dev_server(project_dir: Path) -> None:
    """
    Add an explicit ``server`` block to the generated ``vite.config.ts``.

    The dev container runs ``vite --host``, which binds to all interfaces
    (``::``) inside the container. Without an explicit ``server.hmr.host``,
    laravel-vite-plugin falls back to that raw bind address when writing
    ``public/hot``, producing an URL like ``http://[::]:5173`` that's
    unreachable from the host browser. Laravel then can't detect the dev
    server and falls back to the (nonexistent) production manifest,
    raising ``ViteManifestNotFoundException``. Pinning ``host: '0.0.0.0'``
    and ``hmr.host: 'localhost'`` keeps the container listening everywhere
    while reporting the host-reachable address (via the ``5173:5173`` port
    mapping) for assets/HMR.
    """
    vite_config = project_dir / "vite.config.ts"
    if not vite_config.is_file():
        return
    original = vite_config.read_text()
    if re.search(r"\bserver\s*:", original):
        return
    patched, count = _VITE_DEFINE_CONFIG_RE.subn(
        "defineConfig({\n    " + _VITE_DEV_SERVER_CONFIG, original, count=1
    )
    if count:
        vite_config.write_text(patched)


_WITH_MIDDLEWARE_RE = re.compile(
    r"(->withMiddleware\(function \(Middleware \$middleware\)(?:: void)?\s*\{\n)"
)

_TRUST_PROXIES_SNIPPET = """        // The app container is only ever reached through the Docker
        // reverse proxy (see docker-compose.*.yml), which terminates TLS
        // and forwards plain HTTP with X-Forwarded-* headers. Without
        // trusting it, Laravel sees every request as HTTP and generates
        // insecure (http://) URLs for assets, redirects, etc., and
        // $request->ip() resolves to the proxy instead of the real client.
        $middleware->trustProxies(
            at: '*',
            headers: SymfonyRequest::HEADER_X_FORWARDED_FOR
                | SymfonyRequest::HEADER_X_FORWARDED_HOST
                | SymfonyRequest::HEADER_X_FORWARDED_PORT
                | SymfonyRequest::HEADER_X_FORWARDED_PROTO,
        );

"""

_LAST_USE_STATEMENT_RE = re.compile(r"(^use [^\n]+;\n)(?!use )", re.MULTILINE)


def _configure_trusted_proxies(project_dir: Path) -> None:
    """
    Trust the Docker reverse proxy's forwarded headers in ``bootstrap/app.php``.

    The generated app container is only reachable through an external
    TLS-terminating reverse proxy on the Docker ``proxy-net`` network, which
    forwards plain HTTP. Without ``trustProxies``, ``Request::isSecure()``
    always evaluates to false (causing Vite/route URLs to be generated as
    ``http://`` and get blocked as mixed content once loaded over HTTPS),
    and ``$request->ip()`` resolves to the proxy rather than the real
    client, silently breaking per-IP rate limiting (e.g. Fortify's login
    throttle).
    """
    app_php = project_dir / "bootstrap" / "app.php"
    if not app_php.is_file():
        return
    original = app_php.read_text()
    if "trustProxies" in original:
        return
    patched, count = _WITH_MIDDLEWARE_RE.subn(
        r"\1" + _TRUST_PROXIES_SNIPPET, original, count=1
    )
    if not count:
        return
    if "Symfony\\Component\\HttpFoundation\\Request as SymfonyRequest" not in patched:
        patched, import_count = _LAST_USE_STATEMENT_RE.subn(
            r"\1use Symfony\\Component\\HttpFoundation\\Request as SymfonyRequest;\n",
            patched,
            count=1,
        )
        if not import_count:
            return
    app_php.write_text(patched)


_BOOT_METHOD_RE = re.compile(r"(public function boot\(\): void\s*\{\n)")

_FORCE_SCHEME_SNIPPET = """        // The app container sits behind a reverse proxy that terminates TLS
        // and forwards plain HTTP, so Laravel sees every request as
        // insecure. Force the scheme from APP_URL rather than trusting
        // proxy headers, so generated asset/route URLs stay HTTPS even if
        // the proxy ever fails to forward X-Forwarded-Proto correctly.
        if (str_starts_with(config('app.url'), 'https://')) {
            URL::forceScheme('https');
        }

"""


def _configure_force_https_scheme(project_dir: Path) -> None:
    """
    Force the URL scheme from ``APP_URL`` in ``AppServiceProvider::boot()``.

    Acts as a fallback independent of proxy headers, alongside
    ``_configure_trusted_proxies``: if the reverse proxy ever fails to
    forward ``X-Forwarded-Proto``, generated URLs still stay ``https://``
    whenever ``APP_URL`` itself is HTTPS.
    """
    provider_php = project_dir / "app" / "Providers" / "AppServiceProvider.php"
    if not provider_php.is_file():
        return
    original = provider_php.read_text()
    if "forceScheme" in original:
        return
    patched, count = _BOOT_METHOD_RE.subn(
        r"\1" + _FORCE_SCHEME_SNIPPET, original, count=1
    )
    if not count:
        return
    if "Illuminate\\Support\\Facades\\URL" not in patched:
        patched, import_count = _LAST_USE_STATEMENT_RE.subn(
            r"\1use Illuminate\\Support\\Facades\\URL;\n", patched, count=1
        )
        if not import_count:
            return
    provider_php.write_text(patched)


def _build_env() -> dict[str, str]:
    """Return an os.environ copy augmented with the Composer global bin dir."""
    env = os.environ.copy()
    env.update({"TERM": "dumb", "NO_COLOR": "1"})
    try:
        result = subprocess.run(
            ["composer", "global", "config", "bin-dir", "--absolute"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip():
            env["PATH"] = f"{result.stdout.strip()}:{env.get('PATH', '')}"
    except Exception as exc:
        logger.warning("Could not determine Composer global bin-dir: %s", exc)
    return env


def _run(cmd: list[str], *, cwd: Path | str, env: dict, timeout: int, check: bool = True) -> None:
    subprocess.run(
        cmd,
        cwd=str(cwd),
        check=check,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_docker_zip(upstream_zip_bytes: bytes, context: dict) -> io.BytesIO:
    """
    Re-packages the upstream Laravel source zip, generating an APP_KEY server-side
    and rendering/injecting Docker scaffold files so the archive is ready to run with
    ``docker compose up --build`` — no manual steps required.

    Injected files:
      - Dockerfile
      - docker-compose.yml
      - .env         ← pre-filled, includes a generated APP_KEY
      - .env.docker  ← backup copy
      - README.md
    """
    app_key = generate_app_key()
    ctx = {**context, "app_key": app_key}

    output_buffer = io.BytesIO()

    with zipfile.ZipFile(io.BytesIO(upstream_zip_bytes), "r") as upstream_zip:
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as out_zip:
            for item in upstream_zip.infolist():
                out_zip.writestr(item, upstream_zip.read(item.filename))

            root = upstream_zip.namelist()[0].split("/")[0] + "/"

            for template_path, dest_path in TEMPLATES:
                rendered = _jinja_env.get_template(template_path).render(ctx)
                out_zip.writestr(root + dest_path, rendered)

            # Inject .env (ready to use — no cp step needed) alongside .env.docker
            env_content = _jinja_env.get_template(".env.docker.j2").render(ctx)
            out_zip.writestr(root + ".env", env_content)

    output_buffer.seek(0)
    return output_buffer


async def build_inertia_project_zip(
    context: dict, templates: list[tuple[str, str]] = INERTIA_SERVER_TEMPLATES
) -> io.BytesIO:
    """
    Scaffolds a complete Laravel + Inertia.js project on the server and returns
    a Docker-ready zip. See ``_build_inertia_project_zip_sync`` for the full flow.
    """
    return await asyncio.to_thread(_build_inertia_project_zip_sync, context, templates)


# ---------------------------------------------------------------------------
# Private — synchronous implementation
# ---------------------------------------------------------------------------


def _build_inertia_project_zip_sync(
    context: dict, templates: list[tuple[str, str]] = INERTIA_SERVER_TEMPLATES
) -> io.BytesIO:
    """
    Server-side project generation flow:

    1. ``composer create-project <kit> <name> --no-scripts`` — installs PHP
       deps without running any post-install artisan commands or migrations.
    2. Copy ``.env.example`` → ``.env``, run ``package:discover``,
       ``key:generate``.
    3. If ``testing_framework == "pest"``, swap PHPUnit for Pest
       (``composer remove phpunit/phpunit``, ``composer require pestphp/pest``,
       ``php artisan pest:install``).
    4. ``npm install`` — required before ``install:features`` because chisel's
       ``apply`` callback runs ``npm run lint`` / ``npm run format``.
    5. ``php artisan install:features --no-interaction --answers=<json>`` —
       sculpts the project according to the requested auth features (default: none).
    6. Read the generated APP_KEY; render and write Docker scaffold files.
    7. Overwrite ``.env`` with the Docker-ready environment (DB → Docker service
       hostnames, Redis, etc.).
    8. Zip everything except ``vendor/``, ``node_modules/``, ``.git/``,
       and ``public/build/`` (Vite handles assets at runtime).
    """
    app_name = slugify_app_name(context["app_name"])
    env = _build_env()

    # Optional: install laravel/boost globally
    if context.get("install_boost"):
        _run(
            ["composer", "global", "require", "laravel/boost", "--dev", "--no-interaction", "-q"],
            cwd="/tmp",
            env=env,
            timeout=180,
        )

    package_ref = _starter_kit_ref(
        context["starter_kit"], context["auth_provider"], context["teams"]
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / app_name

        # ── 1. Create project (skip scripts so we control the full flow) ─────
        _run(
            [
                "composer", "create-project", package_ref, app_name,
                "--stability=dev", "--no-interaction", "--no-scripts",
            ],
            cwd=tmp_dir,
            env=env,
            timeout=600,
        )

        # ── 2. Bootstrap Laravel ─────────────────────────────────────────────
        if not (project_dir / ".env").exists():
            shutil.copy(project_dir / ".env.example", project_dir / ".env")

        # Discover packages (replaces post-autoload-dump hook we skipped)
        _run(
            ["php", "artisan", "package:discover", "--ansi"],
            cwd=project_dir,
            env=env,
            timeout=60,
            check=False,
        )
        _run(
            ["php", "artisan", "key:generate", "--ansi"],
            cwd=project_dir,
            env=env,
            timeout=60,
        )

        # ── 3. Swap the test runner to Pest, if requested ─────────────────────
        if context.get("testing_framework") == "pest":
            _run(
                ["composer", "remove", "phpunit/phpunit", "--dev", "--no-interaction"],
                cwd=project_dir,
                env=env,
                timeout=120,
                check=False,
            )
            _run(
                ["composer", "require", "pestphp/pest", "--dev", "--no-interaction", "-W"],
                cwd=project_dir,
                env=env,
                timeout=180,
            )
            _run(
                ["php", "artisan", "pest:install", "--no-interaction"],
                cwd=project_dir,
                env=env,
                timeout=60,
            )

        # ── 4. npm install (chisel apply callback needs node_modules) ─────────
        # Strip remote Google/Bunny font imports first: laravel-vite-plugin
        # self-hosts these by fetching them at build time, and this server
        # has no network route to those font CDNs (see _strip_remote_font_imports).
        _strip_remote_font_imports(project_dir)
        _configure_vite_dev_server(project_dir)
        _configure_trusted_proxies(project_dir)
        _configure_force_https_scheme(project_dir)
        _run(["npm", "install"], cwd=project_dir, env=env, timeout=300)

        # ── 5. Sculpt auth features via chisel ───────────────────────────────
        # Default: no features. Pass explicit list to select specific ones.
        auth_features: list[str] = [
            f for f in context.get("auth_features", []) if f in AUTH_FEATURE_KEYS
        ]
        answers_json = json.dumps({"auth_features": auth_features})
        _run(
            ["php", "artisan", "install:features", "--no-interaction", f"--answers={answers_json}"],
            cwd=project_dir,
            env=env,
            timeout=300,
        )

        # ── 6. Read APP_KEY ───────────────────────────────────────────────────
        app_key = ""
        env_file = project_dir / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("APP_KEY="):
                    app_key = line.split("=", 1)[1].strip()
                    break

        # ── 7. Write Docker scaffold files ────────────────────────────────────
        ctx = {**context, "app_key": app_key, "app_slug": app_name}
        for template_path, dest_path in templates:
            rendered = _jinja_env.get_template(template_path).render(ctx)
            dest = project_dir / dest_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rendered)
            if dest_path.endswith(".sh"):
                dest.chmod(0o755)

        env_docker = _jinja_env.get_template(".env.docker.j2").render(ctx)
        (project_dir / ".env.docker").write_text(env_docker)
        (project_dir / ".env").write_text(env_docker)  # ready for docker compose

        # ── 8. Zip the project ────────────────────────────────────────────────
        _EXCLUDE_TOPS = {"vendor", "node_modules", ".git"}

        output_buffer = io.BytesIO()
        with zipfile.ZipFile(output_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(project_dir.rglob("*")):
                if not file_path.is_file():
                    continue
                rel = file_path.relative_to(project_dir)
                if rel.parts[0] in _EXCLUDE_TOPS:
                    continue
                # Skip Vite build artifacts — the dev container handles assets
                if len(rel.parts) >= 2 and rel.parts[:2] == ("public", "build"):
                    continue
                zf.write(file_path, f"{app_name}/{rel}")

        output_buffer.seek(0)
        return output_buffer

